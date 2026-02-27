from django.urls import path
from django.contrib.auth import views as auth_views
from . import views, research_views, exemplar_views

urlpatterns = [
    # Auth
    path("login/", auth_views.LoginView.as_view(template_name="editor/login.html"), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    # Dashboard
    path("", views.dashboard, name="dashboard"),
    # Document operations
    path("new/", views.new_document, name="new_document"),
    path("create/<slug:type_slug>/", views.create_document, name="create_document"),
    path("editor/<uuid:doc_id>/", views.editor, name="editor"),
    path("delete/<uuid:doc_id>/", views.delete_document, name="delete_document"),
    # API
    path("api/save/<uuid:doc_id>/", views.api_save, name="api_save"),
    path("api/title/<uuid:doc_id>/", views.api_update_title, name="api_update_title"),
    path("api/versions/<uuid:doc_id>/", views.api_versions, name="api_versions"),
    path("api/snapshot/<uuid:doc_id>/", views.api_create_snapshot, name="api_create_snapshot"),
    path(
        "api/restore/<uuid:doc_id>/<int:version_id>/",
        views.api_restore_version,
        name="api_restore_version",
    ),
    # Research API
    path("api/research/suggest/", research_views.suggest_case_law, name="research_suggest"),
    path("api/research/ask/", research_views.ask_question, name="research_ask"),
    path("api/research/categories/", research_views.list_categories, name="research_categories"),
    path("api/research/category/<slug:slug>/", research_views.category_cases_by_slug, name="research_category_slug"),
    path("api/research/category/<int:category_id>/", research_views.category_cases, name="research_category"),
    path("api/research/case/<int:doc_id>/", research_views.case_detail, name="research_case"),
    path("api/research/similar/<int:doc_id>/", research_views.similar_cases, name="research_similar"),
    path("api/research/immcite/<int:doc_id>/", research_views.immcite_status, name="research_immcite"),
    # Exemplar API
    path("api/exemplars/upload/", exemplar_views.exemplar_upload, name="exemplar_upload"),
    path("api/exemplars/search/", exemplar_views.exemplar_search, name="exemplar_search"),
    path("api/exemplars/<int:exemplar_id>/", exemplar_views.exemplar_detail, name="exemplar_detail"),
    path(
        "api/exemplars/suggest/<uuid:doc_id>/",
        exemplar_views.exemplar_suggest_for_document,
        name="exemplar_suggest_document",
    ),
    # Export
    path("export/<uuid:doc_id>/docx/", views.export_docx, name="export_docx"),
    path("export/<uuid:doc_id>/pdf/", views.export_pdf, name="export_pdf"),
]
