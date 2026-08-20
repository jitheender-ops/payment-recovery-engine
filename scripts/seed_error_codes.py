"""Validate error_codes.yaml against FailureClass enum."""
import sys
from pathlib import Path
import yaml
sys.path.insert(0, str(Path(__file__).parent.parent))
from src.classifier.taxonomy import FailureClass

def main():
    yaml_path = Path(__file__).parent.parent / "src" / "classifier" / "error_codes.yaml"
    with open(yaml_path) as f:
        data = yaml.safe_load(f)

    rules = data.get("rules", [])
    valid_classes = {fc.value for fc in FailureClass}
    errors = []
    class_coverage = set()

    for i, rule in enumerate(rules):
        fc = rule.get("failure_class")
        if fc not in valid_classes:
            errors.append(f"Rule {i}: invalid failure_class '{fc}'")
        else:
            class_coverage.add(fc)

    uncovered = valid_classes - class_coverage - {"unknown"}
    print(f"Total rules: {len(rules)}")
    print(f"Valid classes covered: {len(class_coverage)}/{len(valid_classes)-1}")
    if uncovered:
        print(f"⚠️  Uncovered classes: {uncovered}")
    if errors:
        for e in errors:
            print(f"❌ {e}")
    else:
        print("✅ All rules reference valid failure classes")

if __name__ == "__main__":
    main()
