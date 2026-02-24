from django.urls import path
from django.contrib.auth import views as auth_views
from . import views, research_views

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
    # Research API
    path("api/research/suggest/", research_views.suggest_case_law, name="research_suggest"),
    path("api/research/categories/", research_views.list_categories, name="research_categories"),
    path("api/research/category/<int:category_id>/", research_views.category_cases, name="research_category"),
    path("api/research/case/<int:doc_id>/", research_views.case_detail, name="research_case"),
    path("api/research/similar/<int:doc_id>/", research_views.similar_cases, name="research_similar"),
    # Export
    path("export/<uuid:doc_id>/docx/", views.export_docx, name="export_docx"),
]
