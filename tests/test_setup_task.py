import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class SetupTaskStaticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = (ROOT / "setup_task.ps1").read_text(encoding="utf-8")

    def test_missing_config_and_pip_failure_are_fatal(self):
        self.assertIn('throw "配置文件 copartner_ck.json 不存在', self.text)
        self.assertIn('throw "pip 安装失败', self.text)
        self.assertIn('throw "依赖安装后 import 复检失败', self.text)
        self.assertLess(self.text.index('throw "配置文件 copartner_ck.json 不存在'), self.text.index("Register-ScheduledTask"))

    def test_registration_is_checked_before_success_message(self):
        register_index = self.text.index("Register-ScheduledTask")
        query_index = self.text.index("$RegisteredTask = Get-ScheduledTask", register_index)
        validate_index = self.text.index("注册后的参数不匹配", query_index)
        success_index = self.text.index("定时任务已创建并验证!", validate_index)
        catch_index = self.text.index("catch {", success_index)
        failure_exit_index = self.text.index("exit 1", catch_index)
        self.assertLess(register_index, query_index)
        self.assertLess(query_index, validate_index)
        self.assertLess(validate_index, success_index)
        self.assertLess(success_index, catch_index)
        self.assertGreater(failure_exit_index, catch_index)
        self.assertIn("-ErrorAction Stop", self.text[register_index:query_index])

    def test_registered_action_verifies_executable_arguments_and_working_directory(self):
        for property_name in (".Execute", ".Arguments", ".WorkingDirectory"):
            self.assertIn(property_name, self.text)


if __name__ == "__main__":
    unittest.main()
