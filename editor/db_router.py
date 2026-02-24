class BiaEdgeRouter:
    """Route BIA Edge unmanaged models to the biaedge database."""

    biaedge_models = {
        "biadocument",
        "biadocumenttext",
        "biaholding",
        "biaholdingembedding",
        "biacategory",
        "biadocumentcategory",
        "biacitationvalidity",
        "biaheadnote",
    }

    def db_for_read(self, model, **hints):
        if model._meta.model_name in self.biaedge_models:
            return "biaedge"
        return None

    def db_for_write(self, model, **hints):
        if model._meta.model_name in self.biaedge_models:
            return "biaedge"
        return None

    def allow_relation(self, obj1, obj2, **hints):
        return None

    def allow_migrate(self, db, app_label, model_name=None, **hints):
        if model_name and model_name in self.biaedge_models:
            return False
        if db == "biaedge":
            return False
        return None
