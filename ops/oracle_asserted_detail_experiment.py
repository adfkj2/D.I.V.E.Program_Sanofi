"""R26: constrain new detail anchors; retain the R25 retrieval/evidence limits."""
from dataclasses import replace
import oracle_source_retention_experiment as experiment


def main():
    experiment.CANDIDATE = replace(experiment.CANDIDATE, limit=60, reported_detail_assertions_only=True)
    experiment.DEFAULT_OUTPUT = experiment.ROOT / 'eval/reports/oracle-asserted-details-20261008'
    experiment.main()


if __name__ == '__main__':
    main()
