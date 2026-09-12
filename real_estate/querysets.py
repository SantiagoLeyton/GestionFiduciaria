from django.db.models import Case, CharField, F, IntegerField, Q, Value, When
from django.db.models.functions import Length


UNIT_NATURAL_SORT_ANNOTATIONS = (
    "_unit_sort_identifier",
    "_unit_sort_is_numeric",
    "_unit_sort_length",
)


def with_natural_unit_order(queryset, *, include_hierarchy: bool = True):
    identifier = Case(
        When(~Q(code=""), then=F("code")),
        default=F("name"),
        output_field=CharField(),
    )
    is_numeric = Case(
        When(Q(code__regex=r"^\d+$") | (Q(code="") & Q(name__regex=r"^\d+$")), then=Value(0)),
        default=Value(1),
        output_field=IntegerField(),
    )
    queryset = queryset.annotate(
        _unit_sort_identifier=identifier,
        _unit_sort_is_numeric=is_numeric,
        _unit_sort_length=Length(identifier),
    )
    hierarchy = []
    if include_hierarchy:
        hierarchy = [
            "project__name",
            "structural_group__grouping_type__name",
            "structural_group__code",
            "structural_group__name",
        ]
    return queryset.order_by(
        *hierarchy,
        "_unit_sort_is_numeric",
        "_unit_sort_length",
        "_unit_sort_identifier",
        "name",
        "pk",
    )
