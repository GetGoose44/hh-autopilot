"""Пускает ли HH к карточкам вакансий. Печатает ссылку на капчу, если HH её требует, иначе ничего.

После сотен запросов подряд HH отвечает CaptchaRequired на /vacancies/{id},
а без карточки не написать письмо — прогон в таком состоянии только копит ошибки.
"""

from hh_applicant_tool.main import HHApplicantTool

tool = HHApplicantTool()
for key, value in vars(tool._parser.parse_args(["test-session"])).items():
    if not hasattr(type(tool), key):
        setattr(tool, key, value)
try:
    items = tool.api_client.get("/vacancies", {"text": "QA", "per_page": 1})["items"]
    tool.api_client.get(f"/vacancies/{items[0]['id']}")
except Exception as ex:
    if type(ex).__name__ == "CaptchaRequired":
        print(str(ex).split(": ", 1)[-1])
