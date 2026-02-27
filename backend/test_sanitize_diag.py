"""Diagnostic: run raw agent outputs through sanitizer + Pydantic to find failures."""
import json
import sys
import os
import tempfile

# Add backend to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "."))

from app.routers.review import _sanitize_agent_data, _safe_parse
from app.models.schemas import ClinicalResult, CoverageResult, ComplianceResult

TEMP = tempfile.gettempdir()

def test_agent(name, model_class, raw_path):
    print(f"\n{'='*60}")
    print(f"  {name} Agent")
    print(f"{'='*60}")

    with open(raw_path, "r", encoding="utf-8") as f:
        raw = json.load(f)

    print(f"  Raw keys: {list(raw.keys())}")

    sanitized = _sanitize_agent_data(raw)
    print(f"  Sanitized top keys: {list(sanitized.keys())}")

    # Show key fields after sanitization
    if name == "Clinical":
        for key in ("diagnosis_validation", "clinical_extraction", "literature_support",
                     "clinical_trials", "clinical_summary"):
            val = sanitized.get(key)
            if isinstance(val, list):
                print(f"  {key}: list[{len(val)}]", end="")
                if val:
                    first = val[0]
                    if isinstance(first, dict):
                        print(f" — first keys: {list(first.keys())[:5]}")
                    else:
                        print(f" — first type: {type(first).__name__}")
                else:
                    print(" (empty)")
            elif isinstance(val, dict):
                print(f"  {key}: dict keys={list(val.keys())[:5]}")
            elif isinstance(val, str):
                print(f"  {key}: str[{len(val)}] = {val[:80]}...")
            else:
                print(f"  {key}: {type(val).__name__} = {val}")

    elif name == "Coverage":
        for key in ("provider_verification", "coverage_policies", "criteria_assessment",
                     "documentation_gaps"):
            val = sanitized.get(key)
            if isinstance(val, list):
                print(f"  {key}: list[{len(val)}]", end="")
                if val:
                    first = val[0]
                    if isinstance(first, dict):
                        print(f" — first keys: {list(first.keys())[:5]}")
                    else:
                        print(f" — first type: {type(first).__name__}")
                else:
                    print(" (empty)")
            elif isinstance(val, dict):
                print(f"  {key}: dict keys={list(val.keys())[:5]}")
            else:
                print(f"  {key}: {type(val).__name__} = {val}")

    # Try Pydantic parsing
    print(f"\n  --- Pydantic Parsing ---")

    # Stage 1: direct
    try:
        result = model_class.model_validate(sanitized)
        print(f"  Stage 1 (direct): SUCCESS")
        _show_fields(name, result)
        return
    except Exception as e:
        print(f"  Stage 1 (direct): FAILED — {e}")

    # Stage 2: field-by-field
    model_fields = set(model_class.model_fields.keys())
    good_fields = {}
    for field_name in model_fields:
        if field_name not in sanitized:
            continue
        try:
            test_data = {field_name: sanitized[field_name]}
            model_class.model_validate(test_data)
            good_fields[field_name] = sanitized[field_name]
            print(f"    field '{field_name}': OK")
        except Exception as e:
            print(f"    field '{field_name}': FAILED — {e}")

    if good_fields:
        try:
            result = model_class.model_validate(good_fields)
            print(f"  Stage 2 (field-by-field): SUCCESS with {len(good_fields)}/{len([k for k in model_fields if k in sanitized])} fields")
            _show_fields(name, result)
        except Exception as e:
            print(f"  Stage 2 (reassembly): FAILED — {e}")
    else:
        print(f"  Stage 2: No valid fields found!")


def _show_fields(name, result):
    if name == "Clinical":
        r = result
        print(f"    dx codes:        {len(r.diagnosis_validation)}")
        print(f"    extraction:      {r.clinical_extraction is not None}")
        if r.clinical_extraction:
            ce = r.clinical_extraction
            print(f"      chief_complaint:   {ce.chief_complaint[:60] if ce.chief_complaint else '(empty)'}")
            print(f"      hpi:               {ce.history_of_present_illness[:60] if ce.history_of_present_illness else '(empty)'}")
            print(f"      prior_treatments:  {len(ce.prior_treatments)}")
            print(f"      severity_ind:      {len(ce.severity_indicators)}")
            print(f"      diagnostic_find:   {len(ce.diagnostic_findings)}")
            print(f"      extraction_conf:   {ce.extraction_confidence}")
        print(f"    literature:      {len(r.literature_support)}")
        print(f"    trials:          {len(r.clinical_trials)}")
        print(f"    summary:         {r.clinical_summary[:80] if r.clinical_summary else '(empty)'}...")
    elif name == "Coverage":
        r = result
        pv = r.provider_verification
        print(f"    provider name:    {pv.name if pv else 'None'}")
        print(f"    provider spec:    {pv.specialty if pv else 'None'}")
        print(f"    provider status:  {pv.status if pv else 'None'}")
        print(f"    policies:         {len(r.coverage_policies)}")
        print(f"    criteria:         {len(r.criteria_assessment)}")
        print(f"    doc gaps:         {len(r.documentation_gaps)}")
    elif name == "Compliance":
        r = result
        print(f"    checklist:        {len(r.checklist)}")
        print(f"    overall_status:   {r.overall_status}")


if __name__ == "__main__":
    clinical_path = os.path.join(TEMP, "agent_raw_clinical.json")
    coverage_path = os.path.join(TEMP, "agent_raw_coverage.json")
    compliance_path = os.path.join(TEMP, "agent_raw_compliance.json")

    for path, name, model in [
        (compliance_path, "Compliance", ComplianceResult),
        (clinical_path, "Clinical", ClinicalResult),
        (coverage_path, "Coverage", CoverageResult),
    ]:
        if os.path.exists(path):
            test_agent(name, model, path)
        else:
            print(f"\n  {name}: raw file not found at {path}")
