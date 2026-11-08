"""Deprecated module name, kept so an installed unit using
``python -m scripts.vitals_work_delivery`` keeps working. It only stages Vitals now;
point ExecStart at ``scripts.vitals_source`` and delete this file.
"""
from scripts.vitals_source import main, once  # noqa: F401

if __name__ == '__main__':
    main()
