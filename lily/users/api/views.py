import datetime
import django_filters
from django.contrib.sessions.models import Session
from rest_framework import viewsets, status
from rest_framework.authtoken.models import Token
from rest_framework.decorators import list_route
from rest_framework.exceptions import PermissionDenied
from rest_framework.filters import DjangoFilterBackend
from rest_framework.filters import OrderingFilter
from rest_framework.response import Response

from lily.tenant.api.mixins import SetTenantUserMixin
from .serializers import LilyGroupSerializer, LilyUserSerializer, LilyUserTokenSerializer
from ..models import LilyGroup, LilyUser


class TeamFilter(django_filters.FilterSet):
    """
    Class to filter case queryset.
    """

    class Meta:
        model = LilyGroup
        fields = ['name', ]


class TeamViewSet(viewsets.ReadOnlyModelViewSet):
    """
    List all teams assigned to the current user.
    """
    model = LilyGroup
    serializer_class = LilyGroupSerializer
    filter_class = TeamFilter
    queryset = LilyGroup.objects

    def get_queryset(self):
        queryset = self.model.objects.filter(tenant_id=self.request.user.tenant_id)
        return queryset

    def get(self, request, format=None):
        filtered_queryset = self.filter_class(request.GET, queryset=self.get_queryset())
        serializer = self.serializer_class(filtered_queryset, context={'request': request}, many=True)
        return Response(serializer.data)

    @list_route(methods=['GET'])
    def mine(self, request, *args, **kwargs):
        queryset = self.get_queryset().filter(user=self.request.user)
        filtered_queryset = self.filter_class(request.GET, queryset=queryset)
        serializer = self.serializer_class(filtered_queryset, context={'request': request}, many=True)
        return Response(serializer.data)


class LilyUserFilter(django_filters.FilterSet):
    class Meta:
        model = LilyUser
        fields = {
            'is_active': ['exact', ],
        }


class LilyUserViewSet(SetTenantUserMixin, viewsets.ModelViewSet):
    """
        Returns a list of all users in the system, filtered by default on active status.

        #Ordering#
        Ordering is enabled on this API.

        To order, provide a comma seperated list to the ordering argument. Use `-` minus to inverse the ordering.

        #Filtering#
        Filtering is enabled on this API.

        To filter, provide a field name to filter on followed by the value you want to filter on.

        #Examples#
        - plain: `/api/users/user/`
        - order: `/api/users/user/?ordering=first_name,-id`
        - filter: `/api/users/user/?is_active=True`

        #Returns#
        * List of cases with related fields
        """
    # Set the queryset, without .all() this filters on the tenant and takes care of setting the `base_name`.
    queryset = LilyUser.objects
    # Set the serializer class for this viewset.
    serializer_class = LilyUserSerializer
    # Set all filter backends that this viewset uses.
    filter_backends = (OrderingFilter, DjangoFilterBackend)

    # OrderingFilter: set all possible fields to order by.
    ordering_fields = (
        'id', 'first_name', 'preposition', 'last_name', 'email', 'phone_number', 'is_active',
    )
    # OrderingFilter: set the default ordering fields.
    ordering = ('first_name', 'last_name', )
    # DjangoFilter: set the filter class.
    filter_class = LilyUserFilter

    def get_queryset(self):
        """
        Set the queryset here so it filters on tenant and works with pagination.
        """
        # TODO: find out what to do with linked objects when deactivating a user
        # TODO: remove the token page and include it in the normal account page
        queryset = super(LilyUserViewSet, self).get_queryset().filter(
            tenant_id=self.request.user.tenant_id
        ).exclude(
            first_name=''
        )

        # By default we filter out non-active users.
        is_active = self.request.query_params.get('is_active', 'True')

        # Value must be one of these, or it is ignored and we filter out non-active users.
        if is_active not in ['All', 'True', 'False']:
            is_active = 'True'

        # If the value is `All`, do not filter, otherwise filter the queryset on is_active status.
        if is_active in ['True', 'False']:
            queryset = queryset.filter(is_active=(is_active == 'True'))

        return queryset

    def get_object(self):
        """
        Get a user by pk

        If the pk is set to 'me', the currently logged in user will be returned

        Returns:
            serialized user instance
        """
        if self.kwargs['pk'] == 'me':
            return self.request.user
        else:
            return super(LilyUserViewSet, self).get_object()

    @list_route(methods=['GET', 'DELETE', 'POST'])
    def token(self, request, *args, **kwargs):
        """
        This view only returns the token of the currently logged in user

        GET returns the current token
        POST generates a new token
        DELETE removes the current token
        """
        if request.method in ('DELETE', 'POST'):
            Token.objects.filter(user=request.user).delete()
            if request.method == 'DELETE':
                return Response(status=status.HTTP_204_NO_CONTENT)
            else:
                Token.objects.create(user=request.user)

        serializer = LilyUserTokenSerializer(request.user)

        return Response(serializer.data)

    # def update(self, request, *args, **kwargs):
    #     """
    #     Prevent users from updating other users data.
    #     """
    #     if self.request.user != self.get_object():
    #         raise PermissionDenied
    #
    #     return super(LilyUserViewSet, self).update(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        """
        Deactivate a user, but only if it's not the currently logged in user.
        """
        user_to_delete = self.get_object()

        # Prevent the user from deactivating him/herself.
        if request.user == user_to_delete:
            raise PermissionDenied

        user_sessions = []
        all_sessions = Session.objects.filter(expire_date__gte=datetime.datetime.now())

        for session in all_sessions:
            session_data = session.get_decoded()

            if user_to_delete.pk == session_data.get('_auth_user_id'):
                user_sessions.append(session.pk)

        # By using the __in filter we limit the number of queries to 2, instead of a delete query per session.
        Session.objects.filter(pk__in=user_sessions).delete()

        # Don't call super, since that only fires another query using self.get_object().
        self.perform_destroy(user_to_delete)
        return Response(status=status.HTTP_204_NO_CONTENT)
