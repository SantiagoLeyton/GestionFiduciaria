from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth import login, logout
from django.db import transaction
from django.db.models import Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST
from django.views.generic import CreateView, ListView, UpdateView, View

from .forms_admin import ManagedUserCreateForm, ManagedUserUpdateForm, UserSearchForm
from .forms import LoginForm
from .permissions import UserManagementRequiredMixin


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


class UserListView(UserManagementRequiredMixin, ListView):
    model = User
    template_name = "users/user_list.html"
    context_object_name = "users"
    paginate_by = 10

    def get_queryset(self):
        queryset = User.objects.order_by("first_name", "last_name", "username")
        self.search_form = UserSearchForm(self.request.GET)
        if self.search_form.is_valid():
            query = self.search_form.cleaned_data.get("q")
            role = self.search_form.cleaned_data.get("role")
            status = self.search_form.cleaned_data.get("status")
            if query:
                queryset = queryset.filter(
                    Q(first_name__icontains=query)
                    | Q(last_name__icontains=query)
                    | Q(username__icontains=query)
                    | Q(email__icontains=query)
                )
            if role:
                queryset = queryset.filter(role=role)
            if status == "active":
                queryset = queryset.filter(is_active=True)
            elif status == "inactive":
                queryset = queryset.filter(is_active=False)
        return queryset

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["search_form"] = getattr(self, "search_form", UserSearchForm(self.request.GET))
        return context


class UserCreateView(UserManagementRequiredMixin, CreateView):
    model = User
    form_class = ManagedUserCreateForm
    template_name = "users/user_form.html"

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["actor"] = self.request.user
        return kwargs

    def form_valid(self, form):
        with transaction.atomic():
            self.object = form.save()
        messages.success(self.request, "Usuario creado correctamente.")
        return redirect("user_list")

    def form_invalid(self, form):
        messages.error(self.request, "No fue posible guardar el usuario. Revise la informacion ingresada.")
        return super().form_invalid(form)


class UserUpdateView(UserManagementRequiredMixin, UpdateView):
    model = User
    form_class = ManagedUserUpdateForm
    template_name = "users/user_form.html"

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["actor"] = self.request.user
        return kwargs

    def form_valid(self, form):
        with transaction.atomic():
            self.object = form.save()
        messages.success(self.request, "Usuario actualizado correctamente.")
        return redirect("user_list")

    def form_invalid(self, form):
        messages.error(self.request, "No fue posible actualizar el usuario. Revise la informacion ingresada.")
        return super().form_invalid(form)


class UserStatusView(UserManagementRequiredMixin, View):
    def post(self, request, pk, action):
        user = get_object_or_404(User, pk=pk)
        if action not in {"activate", "deactivate"}:
            raise Http404("Accion no disponible.")
        if user.pk == request.user.pk:
            messages.error(request, "No puede cambiar el estado de su propia cuenta.")
            return redirect("user_list")

        user.is_active = action == "activate"
        user.save(update_fields=["is_active"])
        message = "Usuario activado correctamente." if user.is_active else "Usuario inactivado correctamente."
        messages.success(request, message)
        return redirect("user_list")
