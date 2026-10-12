"""R25: widen stored-memory retrieval, keeping the R24 writer and evidence budget."""
from dataclasses import replace
import oracle_source_retention_experiment as experiment


def main():
    experiment.CANDIDATE = replace(experiment.CANDIDATE, limit=60)
    experiment.DEFAULT_OUTPUT = experiment.ROOT / 'eval/reports/oracle-deeper-retrieval-20261007'
    experiment.main()


if __name__ == '__main__':
    main()
