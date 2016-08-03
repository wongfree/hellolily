from django.utils.translation import ugettext_lazy as _
from rest_framework import serializers

from lily.api.nested.mixins import RelatedSerializerMixin
from ..models import LilyGroup, LilyUser


class LilyUserSerializer(serializers.ModelSerializer):
    """
    Serializer for the LilyUser model.
    """
    full_name = serializers.CharField(read_only=True)

    class Meta:
        model = LilyUser
        fields = (
            'id',
            'first_name',
            'preposition',
            'last_name',
            'full_name',
            'primary_email_account',
            'position',
            'profile_picture',
            'picture',
        )

    def update(self, instance, validated_data):
        picture = validated_data.get('picture')

        if not self.partial and not picture:
            # Clear the picture here because it doesn't work in the update of the view.
            validated_data['picture'] = None

        return super(LilyUserSerializer, self).update(instance, validated_data)

    def validate_picture(self, value):
        if value and value.size > 300 * 1024:
            raise serializers.ValidationError(_('File too large. Size should not exceed 300 KB.'))

        return value


class RelatedLilyUserSerializer(RelatedSerializerMixin, LilyUserSerializer):

    class Meta:
        model = LilyUser

        fields = (
            'id',
            'first_name',
            'preposition',
            'last_name',
            'full_name',
        )


class LilyGroupSerializer(serializers.ModelSerializer):
    """
    Serializer for the contact model.
    """
    users = RelatedLilyUserSerializer(many=True, source='active_users')

    class Meta:
        model = LilyGroup
        fields = (
            'id',
            'name',
            'users',
        )


class RelatedLilyGroupSerializer(RelatedSerializerMixin, LilyGroupSerializer):
    class Meta:
        model = LilyGroup
        fields = (
            'id',
            'name',
        )


class LilyUserTokenSerializer(serializers.ModelSerializer):
    """
    Serializer for the LilyUser model.

    Only returns the user token
    """
    auth_token = serializers.CharField(read_only=True)

    class Meta:
        model = LilyUser
        fields = ('auth_token',)
