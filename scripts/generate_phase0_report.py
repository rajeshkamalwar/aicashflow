from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ai_cashflow.reporting import generate_phase0_reports


def main() -> None:
    samples_dir = ROOT / "data" / "samples"
    output_dir = ROOT / "reports" / "phase0"
    generated = generate_phase0_reports(samples_dir, output_dir)
    print(f"Phase 0 reports generated in {generated}")


if __name__ == "__main__":
    main()
