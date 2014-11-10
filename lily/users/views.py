from datetime import date, timedelta
from hashlib import sha256
from uuid import uuid4

import anyjson
from braces.views import StaticContextMixin, GroupRequiredMixin
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, login as user_login
from django.contrib.auth.tokens import default_token_generator, PasswordResetTokenGenerator
from django.contrib.auth.views import login
from django.contrib.auth.models import Group
from django.contrib.messages.views import SuccessMessageMixin
from django.contrib.sites.models import Site
from django.core.paginator import Paginator, PageNotAnInteger, EmptyPage
from django.core.urlresolvers import reverse_lazy, reverse
from django.http import HttpResponse, HttpResponseRedirect
from django.shortcuts import redirect
from django.template import RequestContext
from django.template.loader import render_to_string
from django.views.generic import View, TemplateView, FormView, UpdateView
from django.utils.http import base36_to_int, int_to_base36, urlunquote
from django.utils.translation import ugettext as _
from extra_views import FormSetView
from templated_email import send_templated_mail

from lily.accounts.models import Account
from lily.contacts.models import Contact, Function
from lily.tenant.functions import add_tenant
from lily.updates.forms import CreateBlogEntryForm
from lily.updates.models import BlogEntry
from lily.users.forms import (CustomAuthenticationForm, RegistrationForm, ResendActivationForm, InvitationForm,
                              InvitationFormset, UserRegistrationForm, CustomSetPasswordForm, UserProfileForm,
                              UserAccountForm)
from lily.users.models import LilyUser
from lily.utils.functions import is_ajax
from lily.utils.models import EmailAddress
from lily.utils.views import MultipleModelListView
from lily.utils.views.mixins import LoginRequiredMixin


class RegistrationView(FormView):
    """
    This view shows and handles the registration form, when valid register a new user.
    """
    template_name = 'users/registration.html'
    form_class = RegistrationForm

    def get(self, request, *args, **kwargs):
        # Show a different template when registration is closed.
        if settings.REGISTRATION_POSSIBLE:
            return super(RegistrationView, self).get(request, args, kwargs)
        else:
            self.template_name = 'users/registration_closed.html'
            return self.render_to_response({})

    def form_valid(self, form):
        """
        Register a new user.
        """
        # Do not accept any valid form when registration is closed.
        if not settings.REGISTRATION_POSSIBLE:
            messages.error(self.request, _('Registration is not possible at this moment.'))
            return redirect(reverse_lazy('login'))

        # Create contact
        contact = Contact(
            first_name=form.cleaned_data['first_name'],
            preposition=form.cleaned_data['preposition'],
            last_name=form.cleaned_data['last_name']
        )
        contact, tenant = add_tenant(contact)
        contact.save()

        # Create account
        account = Account(name=form.cleaned_data.get('company'))
        account.tenant = tenant
        account.save()

        # Create function
        Function.objects.create(account=account, contact=contact)

        # Save email
        email = EmailAddress()
        email.email_address = form.cleaned_data['email']
        email.is_primary = True
        email.tenant = tenant
        email.save()
        contact.email_addresses.add(email)
        contact.save()

        # Create and save user
        user = LilyUser()
        user.contact = contact
        user.account = account

        # Store random unique data in username
        user.username = uuid4().get_hex()[:10]
        user.set_password(form.cleaned_data['password'])

        # Set inactive by default, activaten by e-mail required
        user.is_active = False

        user.tenant = tenant
        user.save()

        # Add to admin group
        account_admin = Group.objects.get_or_create(name='account_admin')[0]
        user.groups.add(account_admin)

        # Get the current site
        try:
            current_site = Site.objects.get_current()
        except Site.DoesNotExist:
            current_site = ''

        # Generate uidb36 and token for the activation link
        uidb36 = int_to_base36(user.pk)
        tgen = PasswordResetTokenGenerator()
        token = tgen.make_token(user)

        # Send an activation mail
        # TODO: only create/save contact when e-mail sent succesfully
        send_templated_mail(
            template_name='activation',
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[form.cleaned_data['email']],
            context={
                'current_site': current_site,
                'protocol': self.request.is_secure() and 'https' or 'http',
                'user': user,
                'uidb36': uidb36,
                'token': token,
            }
        )

        # Show registration message
        messages.success(self.request, _('Registration completed. Check your <nobr>e-mail</nobr> to activate your account.'))

        return self.get_success_url()

    def get_success_url(self):
        """
        Redirect to the success url.
        """
        return redirect(reverse_lazy('login'))


class ActivationView(TemplateView):
    """
    This view checks whether the activation link is valid and acts accordingly.
    """
    # Template is only shown when something went wrong
    template_name = 'users/activation_failed.html'
    tgen = PasswordResetTokenGenerator()

    def get(self, request, *args, **kwargs):
        """
        Check whether the activation link is valid, for this both the user id and the token should
        be valid. Messages are shown when user belonging to the user id is already active
        and when the account is successfully activated. In all other cases the activation failed
        template is shown.
        Finally if the user is successfully activated, log user in and redirect to their dashboard.
        """
        try:
            self.uid = base36_to_int(kwargs['uidb36'])
            self.user = LilyUser.objects.get(id=self.uid)
            self.token = kwargs['token']
        except (ValueError, LilyUser.DoesNotExist):
            # Show template as per normal TemplateView behaviour
            return TemplateView.get(self, request, *args, **kwargs)

        if self.tgen.check_token(self.user, self.token):
            # Show activation message
            messages.info(request, _('Your account has been activated and you are now logged in.'))
        else:
            # Show template as per normal TemplateView behaviour
            return TemplateView.get(self, request, *args, **kwargs)

        # Set is_active to True and save the user
        self.user.is_active = True
        self.user.save()

        # Log the user in
        email_address = self.user.primary_email
        self.user = authenticate(username=email_address.email_address, no_pass=True)
        user_login(request, self.user)

        # Redirect to dashboard
        return redirect(reverse_lazy('dashboard'))


class ActivationResendView(FormView):
    """
    This view is used by an user to request a new activation e-mail.
    """
    template_name = 'users/activation_resend_form.html'
    form_class = ResendActivationForm

    def form_valid(self, form):
        """
        If ResendActivationForm passed the validation, generate new token and send an e-mail.
        """
        self.TGen = PasswordResetTokenGenerator()
        self.users = LilyUser.objects.filter(
            contact__email_addresses__email_address__iexact=form.cleaned_data['email'],
            contact__email_addresses__is_primary=True
        )

        # Get the current site or empty string
        try:
            self.current_site = Site.objects.get_current()
        except Site.DoesNotExist:
            self.current_site = ''

        for user in self.users:
            # Generate uidb36 and token for the activation link
            self.uidb36 = int_to_base36(user.pk)
            self.token = self.TGen.make_token(user)

            # E-mail to the user
            send_templated_mail(
                template_name='activation',
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[form.cleaned_data['email']],
                context={
                    'current_site': self.current_site,
                    'protocol': self.request.is_secure() and 'https' or 'http',
                    'full_name': " ".join([user.contact.first_name, user.contact.preposition, user.contact.last_name]),
                    'user': user,
                    'uidb36': self.uidb36,
                    'token': self.token,
                }
            )

        # Show registration message
        messages.success(self.request, _('Reactivation success. Check your <nobr>e-mail</nobr> to activate your account.'))

        # Redirect to success url
        return self.get_success_url()

    def get_success_url(self):
        """
        Redirect to the success url.
        """
        return redirect(reverse_lazy('login'))


class LoginView(View):
    """
    This view extends the default login view with a 'remember me' feature.
    """
    template_name = 'users/login_form.html'

    def dispatch(self, request, *args, **kwargs):
        """
        Check if the user wants to be remembered and return the default login view.
        """
        if request.user.is_authenticated():
            return(redirect(reverse_lazy('dashboard')))

        if request.method == 'POST':
            # If not using 'remember me' feature use default expiration time.
            if not request.POST.get('remember_me', False):
                request.session.set_expiry(None)
        return login(request, template_name=self.template_name, authentication_form=CustomAuthenticationForm, *args, **kwargs)


class SendInvitationView(GroupRequiredMixin, FormSetView):
    """
    This view is used to invite new people to the site. It works with a formset to allow easy
    adding of multiple invitations. It also checks whether the call is done via ajax or via a normal
    form, to use ajax append ?xhr to the url.
    """
    template_name = 'users/invitation/invitation_form.html'
    form_template_name = 'utils/templates/formset_invitation.html'
    form_class = InvitationForm
    formset_class = InvitationFormset
    extra = 1
    can_delete = True
    group_required = ['account_admin', ]

    def formset_valid(self, formset):
        """
        This function is called when the formset is deemed valid.
        An email is sent to all email fields which are filled in.
        If the request is done via ajax give json back with a success message, otherwise
        redirect to the success url.
        """
        self.protocol = self.request.is_secure() and 'https' or 'http'
        self.account = self.request.user.account
        self.b36accountpk = int_to_base36(self.account.pk)
        self.date = date.today().strftime('%d%m%Y')

        # Get the current site or empty string
        try:
            self.current_site = Site.objects.get_current()
        except Site.DoesNotExist:
            self.current_site = ''

        for form in formset:
            if form in formset.deleted_forms:
                continue

            first_name = form.cleaned_data.get('first_name')
            email = form.cleaned_data.get('email')
            if email: # check that the email is not empty
                self.hash = sha256('%s-%s-%s-%s' % (self.account.name, email, self.date, settings.SECRET_KEY)).hexdigest()
                self.invite_link = '%s://%s%s' % (self.protocol, self.current_site, reverse_lazy('invitation_accept', kwargs={
                    'account_name': self.account.name,
                    'first_name': first_name,
                    'email': email,
                    'date': self.date,
                    'aidb36': self.b36accountpk,
                    'hash': self.hash,
                }))

                # E-mail to the user
                send_templated_mail(
                    template_name='invitation',
                    from_email=settings.DEFAULT_FROM_EMAIL,
                    recipient_list=[form.cleaned_data['email']],
                    context={
                        'current_site': self.current_site,
                        'full_name': self.request.user.contact.full_name(),
                        'name': first_name,
                        'invite_link': self.invite_link,
                        'company_name': self.account.name,
                    }
                )

        if is_ajax(self.request):
            return HttpResponse(anyjson.serialize({
                'error': False,
                'html': _('The invitations were sent successfully'),
            }), content_type='application/json')
        return HttpResponseRedirect(self.get_success_url())

    def formset_invalid(self, formset):
        """
        This function is called when the formset didn't pass validation.
        If the request is done via ajax, send back a json object with the error set to true and
        the form rendered into a string.
        """
        if is_ajax(self.request):
            context = RequestContext(self.request, self.get_context_data(formset=formset))
            return HttpResponse(anyjson.serialize({
                'error': True,
                'html': render_to_string(self.form_template_name, context)
            }), content_type='application/json')
        return self.render_to_response(self.get_context_data(formset=formset))

    def get_success_url(self):
        """
        return the success url and set a succes message.
        """
        messages.success(self.request, _('The invitations were sent successfully.'))
        return reverse_lazy('dashboard')


class AcceptInvitationView(FormView):
    """
    This is the view that handles the invitation link and registers the new user if everything
    goes according to plan, otherwise redirect the user to a failure template.
    """
    template_name = 'users/invitation/accept.html'
    template_failure = 'users/invitation/accept_invalid.html'
    form_class = UserRegistrationForm
    valid_link = False

    def dispatch(self, request, *args, **kwargs):
        """
        Set the variables needed and call super.
        This method tries to call dispatch to the right method.
        """
        self.account_name = kwargs.get('account_name')
        self.first_name = kwargs.get('first_name')
        self.email = kwargs.get('email')
        self.datestring = kwargs.get('date')
        self.aidb36 = kwargs.get('aidb36')
        self.hash = kwargs.get('hash')

        return super(AcceptInvitationView, self).dispatch(request, *args, **kwargs)

    def get_template_names(self):
        """
        This method checks if the link is deemed valid, serves appropriate templates.
        """
        if not self.valid_link:
            return [self.template_failure]
        return super(AcceptInvitationView, self).get_template_names()

    def get(self, request, *args, **kwargs):
        """
        This function is called on normal page load. The function link_is_valid is called to
        determine wheter the link is valid. If so load all the necesary data for the form etc.
        otherwise render the failure template (which get_template_names will return since link is
        invalid.
        """
        if self.link_is_valid():
            self.initial = {
                'first_name': self.first_name,
                'email': self.email,
                'company': self.account_name,
            }
            return super(AcceptInvitationView, self).get(request, *args, **kwargs)

        return self.render_to_response(self.get_context_data())

    def post(self, request, *args, **kwargs):
        """
        The function link_is_valid is called to determine if the link is valid.

        If so load all the necessary data for the form etc.
        otherwise render the failure template (which get_template_names will
        return since link is invalid).
        """
        if self.link_is_valid():
            self.initial = {
                'first_name': self.first_name,
                'email': self.email,
                'company': self.account_name,
            }
            return super(AcceptInvitationView, self).post(request, *args, **kwargs)

        return self.render_to_response(self.get_context_data())

    def link_is_valid(self):
        """
        This functions performs all checks to verify the url is correct.

        Returns:
            Boolean: True if link is valid
        """
        # Default value is false, only set to true if all checks have passed
        self.valid_link = False

        if LilyUser.objects.filter(contact__email_addresses__email_address__iexact=self.email).exists():
            return self.valid_link

        try:
            # Check if it's a valid pk and try to retrieve the corresponding account
            self.account = Account.objects.get(pk=base36_to_int(self.aidb36))
        except (ValueError, Account.DoesNotExist):
            return self.valid_link
        else:
            if not self.account.name == self.account_name:
                # The account name from url should be same as in database
                return self.valid_link
            elif not self.hash == sha256('%s-%s-%s-%s' % (self.account.name, self.email, self.datestring, settings.SECRET_KEY)).hexdigest():
                # hash should be correct
                return self.valid_link

        if not len(self.datestring) == 8:
            # Date should always be a string with a length of 8 characters
            return self.valid_link
        else:
            today = date.today()
            try:
                # Check if it is a valid date
                dateobj = date(int(self.datestring[4:8]), int(self.datestring[2:4]), int(self.datestring[:2]))
            except ValueError:
                return self.valid_link
            else:
                if (today < dateobj) or ((today - timedelta(days=settings.USER_INVITATION_TIMEOUT_DAYS)) > dateobj):
                    # Check if the link is not too old and not in the future
                    return self.valid_link

        # Every check was passed successfully, link is valid
        self.valid_link = True
        return self.valid_link

    def form_valid(self, form):
        """
        Create LilyUser with existing Contact or create Contact.
        """
        # Check if there is an existing user, otherwise create one.
        try:
            contact = Contact.objects.filter(
                email_addresses__email_address=self.email,
                email_addresses__is_primary=True,
                tenant=self.account.tenant
            )[0]
        except IndexError:
            contact = Contact(
                first_name=form.cleaned_data['first_name'],
                preposition=form.cleaned_data['preposition'],
                last_name=form.cleaned_data['last_name'],
                tenant=self.account.tenant
            )

            contact.save()
            contact.primary_email = form.cleaned_data['email']
        else:
            contact.first_name = form.cleaned_data['first_name']
            contact.preposition = form.cleaned_data['preposition']
            contact.last_name = form.cleaned_data['last_name']

        contact.save()

        # Create function
        Function.objects.create(account=self.account, contact=contact)

        # Create and save user
        user = LilyUser()
        user.contact = contact
        user.account = self.account
        user.username = uuid4().get_hex()[:10]
        user.set_password(form.cleaned_data['password'])
        user.tenant = self.account.tenant
        user.save()

        return self.get_success_url()

    def get_success_url(self):
        return redirect(reverse_lazy('login'))


class DashboardView(LoginRequiredMixin, MultipleModelListView, TemplateView):
    """
    This view shows the dashboard of the logged in user.
    """
    template_name = 'users/dashboard.html'
    models = [Account, Contact, BlogEntry]
    page_size = 100000000  # TODO: temporarily remove pagination

    def post(self, request, *args, **kwargs):
        """
        Redirect to display a certain page when jumping towards one.
        """
        if 'page' in self.request.POST:
            try:
                location = 'dashboard'
                kwargs = {
                    'page': int(self.request.POST.get('page'))
                }
                if 'tag' in self.request.POST:
                    kwargs.update({'tag': self.request.POST.get('tag')})
                    location += '_tag'

                return redirect(reverse(location, kwargs=kwargs))
            except:
                return redirect(self.request.META['HTTP_REFERER'])

        return super(DashboardView, self).post(request, *args, **kwargs)

    def get_model_queryset(self, list_name, model):
        """
        Return the five newest objects for Accounts and Contacts. Paginate objects for BlogEntry later.
        """
        if model is BlogEntry:
            return model._default_manager.order_by('-created').all().filter(reply_to=None)

        return model._default_manager.order_by('-created').all()[:5]

    def get_context_data(self, **kwargs):
        """
        Paginate the BlogEntry queryset and search for a certain tag when provided.
        """
        context = super(DashboardView, self).get_context_data()
        context.update(kwargs)

        # Paginate BlogEntry
        blogentry_list = context.pop('blogentry_list')

        # Filter by tag?
        if self.kwargs.get('tag', False):
            blogentry_list = BlogEntry.objects.filter(content__contains=urlunquote(self.kwargs.get('tag'))).order_by('-created')

        paginator = Paginator(blogentry_list, self.page_size)

        current_page = self.kwargs.get('page')
        try:
            page = paginator.page(current_page)
        except PageNotAnInteger:
            # If page is not an integer, deliver first page.
            page = paginator.page(1)
        except EmptyPage:
            # If page is out of range (e.g. 9999), deliver last page of results.
            page = paginator.page(paginator.num_pages)

        context.update({
            'paginator': paginator,
            'page': page,
            'blogentry_list': page.object_list,
            'blogentry_form': CreateBlogEntryForm,
            'has_tag_filter': 'tag' in self.kwargs,
            'tag': self.kwargs.get('tag', None),
            'form': CreateBlogEntryForm(self.request.POST or None)
        })

        return context


class CustomSetPasswordView(FormView):
    """
    View that checks the hash in a password reset link and presents a
    form for entering a new password.

    This is a Class-based view copy based on django's default function view password_reset_confirm.
    """
    form_class = CustomSetPasswordForm
    token_generator = default_token_generator
    template_name_invalid = 'users/password_reset/confirm_invalid.html'
    template_name_valid = 'users/password_reset/confirm_valid.html'
    success_url = reverse_lazy('password_reset_complete')

    def dispatch(self, request, *args, **kwargs):
        """
        Overload super().dispatch to verify the reset link before rendering the response.
        """
        self.is_valid_link, self.user = self.check_valid_link(**kwargs)

        return super(CustomSetPasswordView, self).dispatch(request, *args, **kwargs)

    def get_form_kwargs(self):
        """
        Update the keyword arguments for instanciating the form to include the user.
        """
        kwargs = super(CustomSetPasswordView, self).get_form_kwargs()
        kwargs.update({
            'user': self.user
        })

        return kwargs

    def check_valid_link(self, **kwargs):
        """
        Check the url is a valid password reset link.
        """
        uidb36 = kwargs.pop('uidb36')
        token = kwargs.pop('token')

        assert uidb36 is not None and token is not None # checked by URLconf
        try:
            uid_int = base36_to_int(uidb36)
            user = LilyUser.objects.get(id=uid_int)
        except (ValueError, LilyUser.DoesNotExist):
            user = None

        if user is not None and self.token_generator.check_token(user, token):
            return True, user

        return False, user

    def get_template_names(self):
        """
        Overload super().get_template_names to conditionally return different templates.
        """
        if self.is_valid_link:
            template_name = self.template_name_valid
        else:
            template_name = self.template_name_invalid

        return [template_name]

    def form_valid(self, form):
        """
        Overload super().form_valid to save the password change.
        """
        form.save()
        return super(CustomSetPasswordView, self).form_valid(form)


class UserProfileView(LoginRequiredMixin, SuccessMessageMixin, StaticContextMixin, UpdateView):
    form_class = UserProfileForm
    template_name = 'profile.html'
    static_context = {'form_prevent_autofill': True}

    def get_success_message(self, cleaned_data):
        return _('%(name)s has been updated' % {'name': self.object.get_full_name()})

    def get_object(self, queryset=None):
        return self.request.user

    def get_success_url(self):
        return reverse('user_profile_view')


class UserAccountView(LoginRequiredMixin, SuccessMessageMixin, StaticContextMixin, UpdateView):
    form_class = UserAccountForm
    template_name = 'users/user_account_form.html'
    static_context = {'form_prevent_autofill': True}

    def get_success_message(self, cleaned_data):
        return _('%(name)s has been updated' % {'name': self.object.get_full_name()})

    def get_object(self, queryset=None):
        return self.request.user

    def get_success_url(self):
        return reverse('user_account_view')
