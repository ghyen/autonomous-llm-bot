import os
import tempfile
import unittest
from types import SimpleNamespace

from test_support import FakeMessage  # sets required config env before bot imports
import bot


class WorkspaceSkillsTest(unittest.TestCase):
    def test_discover_skills_empty_when_directory_missing_or_empty(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SimpleNamespace(root=temp_dir)
            skills = bot.discover_workspace_skills(workspace)
            self.assertEqual(skills, [])

            skills_block = bot.render_skills_block(workspace)
            self.assertIn("재사용 가능한 작업 공간 스킬", skills_block)
            self.assertIn("등록된 스킬 없음", skills_block)

    def test_discover_skills_extracts_metadata_from_py_sh_and_md(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SimpleNamespace(root=temp_dir)
            skills_dir = os.path.join(workspace.root, "skills")
            os.makedirs(skills_dir, exist_ok=True)

            py_file = os.path.join(skills_dir, "parse_logs.py")
            with open(py_file, "w", encoding="utf-8") as f:
                f.write('"""로그 파일을 파싱하여 에러 목록을 추출합니다."""\nimport sys\nprint("parsed")\n')

            sh_file = os.path.join(skills_dir, "fetch_metrics.sh")
            with open(sh_file, "w", encoding="utf-8") as f:
                f.write('#!/bin/bash\n# Description: 메트릭 엔드포인트에서 CPU/메모리 정보를 조회합니다.\ncurl -s http://localhost/metrics\n')

            md_file = os.path.join(skills_dir, "audit_guide.md")
            with open(md_file, "w", encoding="utf-8") as f:
                f.write('# 보안 감사 절차 가이드\n1. 포트 스캔\n2. 취약점 분석\n')

            # Unrelated / hidden file should be ignored
            hidden_file = os.path.join(skills_dir, ".hidden.py")
            with open(hidden_file, "w", encoding="utf-8") as f:
                f.write('# hidden\n')

            skills = bot.discover_workspace_skills(workspace)
            self.assertEqual(len(skills), 3)

            skills_by_name = {s["name"]: s for s in skills}
            self.assertIn("skills/parse_logs.py", skills_by_name)
            self.assertEqual(skills_by_name["skills/parse_logs.py"]["description"], "로그 파일을 파싱하여 에러 목록을 추출합니다.")
            self.assertEqual(skills_by_name["skills/parse_logs.py"]["type"], "python")

            self.assertIn("skills/fetch_metrics.sh", skills_by_name)
            self.assertEqual(skills_by_name["skills/fetch_metrics.sh"]["description"], "메트릭 엔드포인트에서 CPU/메모리 정보를 조회합니다.")
            self.assertEqual(skills_by_name["skills/fetch_metrics.sh"]["type"], "shell")

            self.assertIn("skills/audit_guide.md", skills_by_name)
            self.assertEqual(skills_by_name["skills/audit_guide.md"]["type"], "markdown")

            rendered = bot.render_skills_block(workspace)
            self.assertIn("skills/parse_logs.py", rendered)
            self.assertIn("로그 파일을 파싱하여 에러 목록을 추출합니다.", rendered)
            self.assertIn("skills/fetch_metrics.sh", rendered)
            self.assertIn("bash_exec", rendered)

    def test_build_system_content_includes_skills_block(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SimpleNamespace(root=temp_dir)
            skills_dir = os.path.join(workspace.root, "skills")
            os.makedirs(skills_dir, exist_ok=True)
            tool_path = os.path.join(skills_dir, "custom_tool.py")
            with open(tool_path, "w", encoding="utf-8") as f:
                f.write('"""전용 데이터 추출 도구"""\n')

            content = bot.build_system_content(workspace)
            self.assertIn("재사용 가능한 작업 공간 스킬", content)
            self.assertIn("skills/custom_tool.py", content)
            self.assertIn("전용 데이터 추출 도구", content)


if __name__ == "__main__":
    unittest.main()
