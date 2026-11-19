"""Private calculation implementation omitted; no coefficients or formulas distributed."""
from fastapi import HTTPException

STAGE5_CARBON_PIPELINE_NAME = "stage5_carbon_assessment"
STAGE5_CARBON_AUDIT_PIPELINE_NAME = "stage5_carbon_assessment_audit"
CARBON_ASSESSMENT_PIPELINES = (STAGE5_CARBON_PIPELINE_NAME, "stage3_carbon_assessment")
CARBON_AUDIT_PIPELINES = (STAGE5_CARBON_AUDIT_PIPELINE_NAME, "stage3_carbon_assessment_audit")

def default_stage5_config_path(*args, **kwargs):
    raise HTTPException(status_code=501, detail="Private calculation service is not distributed.")

def load_stage5_config(*args, **kwargs):
    raise HTTPException(status_code=501, detail="Private calculation service is not distributed.")

def load_stage5_effective_config(*args, **kwargs):
    raise HTTPException(status_code=501, detail="Private calculation service is not distributed.")

def validate_stage5_config(*args, **kwargs):
    raise HTTPException(status_code=501, detail="Private calculation service is not distributed.")

def active_stage5_rule_record(*args, **kwargs):
    raise HTTPException(status_code=501, detail="Private calculation service is not distributed.")

def stage5_merge_config(*args, **kwargs):
    raise HTTPException(status_code=501, detail="Private calculation service is not distributed.")

def sanitize_stage5_config_patch(*args, **kwargs):
    raise HTTPException(status_code=501, detail="Private calculation service is not distributed.")

def stage5_methodology(*args, **kwargs):
    raise HTTPException(status_code=501, detail="Private calculation service is not distributed.")

def stage5_scope_params(region_code=None, grid_version=None):
    from app.api.stage1 import stage1_params
    from app.core.config import get_settings
    settings = get_settings()
    return stage1_params(region_code=region_code or settings.stage4_region_code, grid_version=grid_version or settings.stage4_grid_version)

def stage5_coefficient_set(*args, **kwargs):
    raise HTTPException(status_code=501, detail="Private calculation service is not distributed.")

def stage5_annual_increment_set(*args, **kwargs):
    raise HTTPException(status_code=501, detail="Private calculation service is not distributed.")

def stage5_anomaly_rules(*args, **kwargs):
    raise HTTPException(status_code=501, detail="Private calculation service is not distributed.")

def stage5_confidence_score_config(*args, **kwargs):
    raise HTTPException(status_code=501, detail="Private calculation service is not distributed.")

def stage5_statistical_model(*args, **kwargs):
    raise HTTPException(status_code=501, detail="Private calculation service is not distributed.")

def stage5_annual_increment_config(*args, **kwargs):
    raise HTTPException(status_code=501, detail="Private calculation service is not distributed.")

def stage5_public_config(*args, **kwargs):
    raise HTTPException(status_code=501, detail="Private calculation service is not distributed.")

def stage5_annual_increment_query_params(*args, **kwargs):
    """Stored-column reads require no private calculation parameters."""
    return {}

def _public_methodology(*args, **kwargs):
    raise HTTPException(status_code=501, detail="Private calculation service is not distributed.")

def _public_annual_increment(*args, **kwargs):
    raise HTTPException(status_code=501, detail="Private calculation service is not distributed.")

def _public_external_inputs(*args, **kwargs):
    raise HTTPException(status_code=501, detail="Private calculation service is not distributed.")

def _decimal_rule(*args, **kwargs):
    raise HTTPException(status_code=501, detail="Private calculation service is not distributed.")

def _decimal(*args, **kwargs):
    raise HTTPException(status_code=501, detail="Private calculation service is not distributed.")

def _bool_rule(*args, **kwargs):
    raise HTTPException(status_code=501, detail="Private calculation service is not distributed.")

def _validate_scalar_hierarchy(*args, **kwargs):
    raise HTTPException(status_code=501, detail="Private calculation service is not distributed.")

def _validate_scalar_override_group(*args, **kwargs):
    raise HTTPException(status_code=501, detail="Private calculation service is not distributed.")

def _stage5_rule_table_exists(*args, **kwargs):
    raise HTTPException(status_code=501, detail="Private calculation service is not distributed.")

def _sanitize_json_patch(*args, **kwargs):
    raise HTTPException(status_code=501, detail="Private calculation service is not distributed.")

def _sanitize_json_patch_value(*args, **kwargs):
    raise HTTPException(status_code=501, detail="Private calculation service is not distributed.")

def _reject_sensitive_rule_key(*args, **kwargs):
    raise HTTPException(status_code=501, detail="Private calculation service is not distributed.")

def _is_sensitive_rule_key(*args, **kwargs):
    raise HTTPException(status_code=501, detail="Private calculation service is not distributed.")

def _jsonb_record(*args, **kwargs):
    raise HTTPException(status_code=501, detail="Private calculation service is not distributed.")

def _jsonable(*args, **kwargs):
    raise HTTPException(status_code=501, detail="Private calculation service is not distributed.")

