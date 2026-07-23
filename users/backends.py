from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend
from django.db.models import Q


class UsernameOrEmailBackend(ModelBackend):
    def authenticate(self, request, username=None, password=None, **kwargs):
        identifier = username or kwargs.get("email")
        if identifier:
            identifier = identifier.strip()
        if not identifier or not password:
            return None

        UserModel = get_user_model()
        try:
            user = UserModel.objects.get(Q(username__iexact=identifier) | Q(email__iexact=identifier))
        except UserModel.DoesNotExist:
            UserModel().set_password(password)
            return None
        except UserModel.MultipleObjectsReturned:
            return None

        if user.check_password(password) and self.user_can_authenticate(user):
            return user
        return None
