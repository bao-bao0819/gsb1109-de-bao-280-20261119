from django.contrib import admin
from django.core.exceptions import ValidationError

from import_export.admin import (
    ExportActionModelAdmin,
    ImportExportModelAdmin,
    ImportMixin,
)
from import_export.fields import Field
from import_export.resources import ModelResource

from .forms import CustomConfirmImportForm, CustomExportForm, CustomImportForm
from .models import (
    Author,
    Book,
    Category,
    Child,
    EBook,
    Supplier,
    UUIDBook,
    UUIDCategory,
)


@admin.register(Child)
class ChildAdmin(ImportMixin, admin.ModelAdmin):
    pass


class BookResource(ModelResource):

    class Meta:
        model = Book

    def for_delete(self, row, instance):
        return self.fields["name"].clean(row) == ""


class BookNameResource(ModelResource):
    class Meta:
        model = Book
        fields = ["id", "name"]
        name = "Export/Import only book names"


class AuthorBirthdayListFilter(admin.SimpleListFilter):
    """Example filter which can be used in the Admin UI to filter books by
    author birthday."""

    title = "author's birthday"
    parameter_name = "birthday"

    def lookups(self, request, model_admin):
        return (
            ("before", "before 1900"),
            ("after", "after 1900"),
        )

    def choices(self, cl):
        for lookup, title in self.lookup_choices:
            yield {
                "selected": self.value() == lookup,
                "query_string": cl.get_query_string({self.parameter_name: lookup}, []),
                "display": title,
            }

    def queryset(self, request, queryset):
        if self.value() == "before":
            return queryset.filter(author__birthday__year__lt=1900)
        if self.value() == "after":
            return queryset.filter(author__birthday__year__gte=1900)
        return queryset


@admin.register(Book)
class BookAdmin(ExportActionModelAdmin, ImportExportModelAdmin):
    list_display = ("name", "author", "added")
    list_filter = [AuthorBirthdayListFilter, "categories", "author"]
    resource_classes = [BookResource, BookNameResource]
    change_list_template = "core/admin/change_list.html"


@admin.register(Category)
class CategoryAdmin(ExportActionModelAdmin):
    def get_queryset(self, request):
        return Category.objects.all()


@admin.register(UUIDBook)
class UUIDBookAdmin(ImportExportModelAdmin):
    pass


@admin.register(UUIDCategory)
class UUIDCategoryAdmin(ExportActionModelAdmin):
    pass


@admin.register(Author)
class AuthorAdmin(ImportExportModelAdmin, ExportActionModelAdmin, admin.ModelAdmin):
    pass


class UUIDBookResource(ModelResource):
    class Meta:
        model = UUIDBook


class SupplierResource(ModelResource):
    """Resource for the supplier CSV regression tests.

    The legacy ``store_id`` header is accepted as an alias of ``store_no``.
    Alias normalisation and the duplicate/conflict checks run in
    :meth:`before_import` against the raw :class:`tablib.Dataset`, because
    that is the last point where the physical column positions are still
    observable: afterwards each row is collapsed into an ``OrderedDict``
    keyed by header name, so two columns mapping to the same field can no
    longer be told apart.
    """

    #: historical header which is normalised onto the ``store_no`` column
    store_no_alias = "store_id"

    order_no = Field(attribute="order_no", column_name="order_no")
    store_no = Field(attribute="store_no", column_name="store_no")

    class Meta:
        model = Supplier
        fields = ("id", "order_no", "store_no")

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        #: headers seen by the resource after alias normalisation, in file order
        self.canonical_headers = []
        #: every row handed to the resource during import
        self.received_rows = []

    def before_import(self, dataset, **kwargs):
        raw_headers = list(dataset.headers or [])
        canonical_headers = [
            "store_no" if header == self.store_no_alias else header
            for header in raw_headers
        ]
        self.canonical_headers = canonical_headers

        # Two physical columns resolving to the same canonical header are
        # ambiguous: this covers both an alias/canonical pair
        # (store_id + store_no) and an exact raw duplicate (store_no twice).
        first_position = {}
        for position, canonical in enumerate(canonical_headers, start=1):
            if canonical in first_position:
                first = first_position[canonical]
                raise ValidationError(
                    "Ambiguous column mapping after alias normalisation: "
                    "columns %d and %d (headers %r and %r) both resolve to %r"
                    % (
                        first,
                        position,
                        raw_headers[first - 1],
                        raw_headers[position - 1],
                        canonical,
                    )
                )
            first_position[canonical] = position

        if canonical_headers != raw_headers:
            dataset.headers = canonical_headers
        super().before_import(dataset, **kwargs)

    def before_import_row(self, row, **kwargs):
        self.received_rows.append(dict(row))
        super().before_import_row(row, **kwargs)


@admin.register(Supplier)
class SupplierAdmin(ImportExportModelAdmin):
    resource_classes = [SupplierResource]


class EBookResource(ModelResource):
    published = Field(attribute="published", column_name="published_date")
    author_email = Field(attribute="author_email", column_name="Email of the author")
    auteur_name = Field(attribute="author__name", column_name="Author Name")

    def __init__(self, **kwargs):
        super().__init__()
        self.author_id = kwargs.get("author_id")

    def filter_export(self, queryset, **kwargs):
        return queryset.filter(author_id=self.author_id)

    class Meta:
        model = EBook
        fields = ("id", "author_email", "name", "published", "auteur_name")


@admin.register(EBook)
class CustomBookAdmin(ExportActionModelAdmin, ImportExportModelAdmin):
    """Example usage of custom import / export forms"""

    resource_classes = [EBookResource]
    import_form_class = CustomImportForm
    confirm_form_class = CustomConfirmImportForm
    export_form_class = CustomExportForm

    def get_confirm_form_initial(self, request, import_form):
        initial = super().get_confirm_form_initial(request, import_form)
        # Pass on the `author` value from the import form to
        # the confirm form (if provided)
        if import_form:
            initial["author"] = import_form.cleaned_data["author"].id
        return initial

    def get_import_resource_kwargs(self, request, **kwargs):
        # update resource kwargs so that the Resource is passed the authenticated user
        # This is included as an example of how dynamic values
        # can be passed to resources
        if "form" not in kwargs:
            # test for #1789
            raise ValueError("'form' param was expected in kwargs")
        kwargs = super().get_resource_kwargs(request, **kwargs)
        kwargs.update({"user": request.user})
        return kwargs

    def get_export_resource_kwargs(self, request, **kwargs):
        # this is overridden to demonstrate that custom form fields can be used
        # to override the export query.
        # The dict returned here will be passed as kwargs to EBookResource
        export_form = kwargs.get("export_form")
        if export_form:
            kwargs.update(author_id=export_form.cleaned_data["author"].id)
        return kwargs
