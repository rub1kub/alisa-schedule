import argparse

from app.config import Settings
from app.models import College, Overrides, ScheduleFile, read_json
from app.responses import Responses


def main():
    parser = argparse.ArgumentParser(description="Validate skill data without contacting KKEPIK")
    parser.add_argument("--with-example", action="store_true")
    args = parser.parse_args()
    settings = Settings.from_env()
    College.model_validate(read_json(settings.college_file))
    Responses.load(settings.responses_file)
    Overrides.model_validate(read_json(settings.overrides_file))
    if args.with_example or settings.provider == "file":
        ScheduleFile.model_validate(read_json(settings.schedule_file))
    print("Data validation: OK")


if __name__ == "__main__":
    main()
