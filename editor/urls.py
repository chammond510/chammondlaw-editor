from django.urls import path
from django.contrib.auth import views as auth_views
from . import views

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
    # Export
    path("export/<uuid:doc_id>/docx/", views.export_docx, name="export_docx"),
]
