from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth import login, logout
from django.contrib.admin.models import ADDITION, CHANGE, DELETION, LogEntry
from django.db.models import Q
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, reverse_lazy
from django.utils.crypto import get_random_string
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST
from django.views.generic import CreateView, ListView, UpdateView, View

from .forms_admin import ManagedUserCreateForm, ManagedUserUpdateForm, UserActionReasonForm, UserSearchForm
from .forms import LoginForm
from .permissions import UserManagementRequiredMixin, UserReadRequiredMixin


User = get_user_model()


class LoginView(View):
    template_name = "users/login.html"

    def get(self, request):
        if request.user.is_authenticated:
            return redirect("home")
        return render(request, self.template_name, {"form": LoginForm(request)})

    def post(self, request):
        form = LoginForm(request, data=request.POST)
        if form.is_valid():
            login(request, form.get_user())
            if not form.cleaned_data.get("remember_me"):
                request.session.set_expiry(0)
            messages.success(request, "Inicio de sesion exitoso.")
            next_url = request.POST.get("next") or request.GET.get("next")
            if next_url and url_has_allowed_host_and_scheme(
                next_url,
                allowed_hosts={request.get_host()},
                require_https=request.is_secure(),
            ):
                return redirect(next_url)
            return redirect("home")
        messages.error(request, "No fue posible iniciar sesion. Verifique sus credenciales.")
        return render(request, self.template_name, {"form": LoginForm(request)}, status=400)


@require_POST
def logout_view(request):
    logout(request)
    messages.info(request, "Sesion cerrada correctamente.")
    return redirect(reverse("login"))


class UserListView(UserReadRequiredMixin, ListView):
    model = User
    template_name = "users/user_list.html"
    context_object_name = "users"
    paginate_by = 10

    def get_queryset(self):
        queryset = User.objects.order_by("first_name", "last_name", "email")
        self.search_form = UserSearchForm(self.request.GET)
        if self.search_form.is_valid():
            query = self.search_form.cleaned_data.get("q")
            role = self.search_form.cleaned_data.get("role")
            status = self.search_form.cleaned_data.get("status")
            if query:
                queryset = queryset.filter(
                    Q(first_name__icontains=query)
                    | Q(last_name__icontains=query)
                    | Q(email__icontains=query)
                )
            if role:
                queryset = queryset.filter(role=role)
            if status == "active":
                queryset = queryset.filter(is_active=True, is_deleted=False)
            elif status == "inactive":
                queryset = queryset.filter(is_active=False, is_deleted=False)
            elif status == "deleted":
                queryset = queryset.filter(is_deleted=True)
            else:
                queryset = queryset.filter(is_deleted=False)
        else:
            queryset = queryset.filter(is_deleted=False)
        return queryset

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["search_form"] = getattr(self, "search_form", UserSearchForm(self.request.GET))
        query_params = self.request.GET.copy()
        query_params.pop("page", None)
        context["page_querystring"] = query_params.urlencode()
        return context


class ManagedUserCreateView(UserManagementRequiredMixin, CreateView):
    model = User
    form_class = ManagedUserCreateForm
    template_name = "users/user_form.html"
    success_url = reverse_lazy("user_list")

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["actor"] = self.request.user
        return kwargs

    def form_valid(self, form):
        reused_user = getattr(form, "deleted_user_for_reuse", None)
        if reused_user:
            user = reused_user
            user.first_name = form.cleaned_data["first_name"]
            user.last_name = form.cleaned_data["last_name"]
            user.email = form.cleaned_data["email"]
            user.role = form.cleaned_data["role"]
            user.is_active = form.cleaned_data["is_active"]
            user.is_deleted = False
            user.deleted_at = None
            user.username = _generate_internal_username(user.email, exclude_pk=user.pk)
            user.set_password(get_random_string(48))
            user.groups.clear()
            user.user_permissions.clear()
            user.save(
                update_fields=[
                    "first_name",
                    "last_name",
                    "email",
                    "role",
                    "is_active",
                    "is_deleted",
                    "deleted_at",
                    "username",
                    "password",
                ]
            )
            self.object = user
            _log_user_action(
                self.request.user,
                self.object,
                ADDITION,
                "Cuenta recreada desde Gestion de cuentas reutilizando correo de usuario eliminado logicamente.",
            )
            messages.success(
                self.request,
                "Cuenta creada correctamente. El usuario debe utilizar la recuperacion de contrasena para definir su clave.",
            )
            return redirect(self.success_url)
        user = form.save(commit=False)
        user.username = _generate_internal_username(user.email)
        user.set_password(get_random_string(48))
        user.is_deleted = False
        user.deleted_at = None
        response = super().form_valid(form)
        _log_user_action(self.request.user, self.object, ADDITION, "Cuenta creada desde Gestion de cuentas.")
        messages.success(
            self.request,
            "Cuenta creada correctamente. El usuario debe utilizar la recuperacion de contrasena para definir su clave.",
        )
        return response


class ManagedUserUpdateView(UserManagementRequiredMixin, UpdateView):
    model = User
    form_class = ManagedUserUpdateForm
    template_name = "users/user_form.html"
    success_url = reverse_lazy("user_list")

    def get_queryset(self):
        return User.objects.filter(is_deleted=False)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["actor"] = self.request.user
        return kwargs

    def form_valid(self, form):
        response = super().form_valid(form)
        _log_user_action(self.request.user, self.object, CHANGE, "Cuenta actualizada desde Gestion de cuentas.")
        messages.success(self.request, "Cuenta actualizada correctamente.")
        return response


class ManagedUserStatusView(UserManagementRequiredMixin, View):
    allowed_actions = {"activate", "deactivate"}
    template_name = "users/user_action_confirm.html"

    def get(self, request, pk, action):
        if action != "deactivate":
            raise PermissionDenied
        user = get_object_or_404(User, pk=pk, is_deleted=False)
        if user.pk == request.user.pk:
            messages.error(request, "No puede inactivar su propia cuenta desde esta pantalla.")
            return redirect("user_list")
        return render(
            request,
            self.template_name,
            {
                "form": UserActionReasonForm(),
                "managed_user": user,
                "title": "Inactivar usuario",
                "message": "El usuario no podra iniciar sesion mientras permanezca inactivo.",
                "confirm_label": "Inactivar",
                "confirm_class": "btn-outline-secondary",
            },
        )

    def post(self, request, pk, action):
        if action not in self.allowed_actions:
            raise PermissionDenied
        user = get_object_or_404(User, pk=pk, is_deleted=False)
        if user.pk == request.user.pk and action == "deactivate":
            messages.error(request, "No puede inactivar su propia cuenta desde esta pantalla.")
            return redirect("user_list")
        if action == "deactivate":
            form = UserActionReasonForm(request.POST)
            if not form.is_valid():
                return render(
                    request,
                    self.template_name,
                    {
                        "form": form,
                        "managed_user": user,
                        "title": "Inactivar usuario",
                        "message": "El usuario no podra iniciar sesion mientras permanezca inactivo.",
                        "confirm_label": "Inactivar",
                        "confirm_class": "btn-outline-secondary",
                    },
                    status=400,
                )
            reason = form.cleaned_data["reason"]
        else:
            reason = ""
        user.is_active = action == "activate"
        user.save(update_fields=["is_active"])
        label = "activada" if user.is_active else "inactivada"
        if action == "deactivate":
            _log_user_action(request.user, user, CHANGE, f"INACTIVAR_USUARIO | Cuenta {label} desde Gestion de cuentas. Motivo: {reason}")
        else:
            _log_user_action(request.user, user, CHANGE, f"Cuenta {label} desde Gestion de cuentas.")
        messages.success(request, f"Cuenta {label} correctamente.")
        return redirect("user_list")


class ManagedUserDeleteView(UserManagementRequiredMixin, View):
    template_name = "users/user_action_confirm.html"

    def get(self, request, pk):
        user = get_object_or_404(User, pk=pk, is_deleted=False)
        if user.pk == request.user.pk:
            messages.error(request, "No puede eliminar logicamente su propia cuenta desde esta pantalla.")
            return redirect("user_list")
        return render(
            request,
            self.template_name,
            {
                "form": UserActionReasonForm(),
                "managed_user": user,
                "title": "Eliminar usuario",
                "message": "La cuenta sera eliminada logicamente y dejara de estar disponible. La informacion historica y de auditoria debe conservarse.",
                "confirm_label": "Eliminar",
                "confirm_class": "btn-outline-danger",
            },
        )

    def post(self, request, pk):
        user = get_object_or_404(User, pk=pk, is_deleted=False)
        if user.pk == request.user.pk:
            messages.error(request, "No puede eliminar logicamente su propia cuenta desde esta pantalla.")
            return redirect("user_list")
        form = UserActionReasonForm(request.POST)
        if not form.is_valid():
            return render(
                request,
                self.template_name,
                {
                    "form": form,
                    "managed_user": user,
                    "title": "Eliminar usuario",
                    "message": "La cuenta sera eliminada logicamente y dejara de estar disponible. La informacion historica y de auditoria debe conservarse.",
                    "confirm_label": "Eliminar",
                    "confirm_class": "btn-outline-danger",
                },
                status=400,
            )
        user.mark_deleted()
        user.save(update_fields=["is_deleted", "is_active", "deleted_at"])
        _log_user_action(
            request.user,
            user,
            DELETION,
            f"ELIMINAR_USUARIO | Cuenta eliminada logicamente desde Gestion de cuentas. Motivo: {form.cleaned_data['reason']}",
        )
        messages.success(request, "Cuenta eliminada logicamente. El historial asociado se conserva.")
        return redirect("user_list")


BlockedUserManagementView = ManagedUserCreateView


def _generate_internal_username(email, *, exclude_pk=None):
    base = (email.split("@", 1)[0] or "usuario").lower()
    base = "".join(character if character.isalnum() or character in "._+-" else "_" for character in base)
    base = base[:120] or "usuario"
    candidate = base
    suffix = 1
    queryset = User.objects.all()
    if exclude_pk:
        queryset = queryset.exclude(pk=exclude_pk)
    while queryset.filter(username__iexact=candidate).exists():
        suffix += 1
        candidate = f"{base[:120]}_{suffix}"
    return candidate


def _log_user_action(actor, target_user, action_flag, message):
    LogEntry.objects.log_actions(
        user_id=actor.pk,
        queryset=User.objects.filter(pk=target_user.pk),
        action_flag=action_flag,
        change_message=message,
        single_object=True,
    )
