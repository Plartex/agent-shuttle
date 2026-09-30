"""Catch PowerShell syntax errors before startup/shutdown scripts are shipped."""

import shutil
import subprocess
import unittest
from pathlib import Path


class PowerShellScriptTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("pwsh"), "PowerShell 7 is not installed")
    def test_bridge_lifecycle_scripts_parse(self):
        root = Path(__file__).resolve().parents[1]
        for name in (
            "Start-Bridge.ps1", "Stop-Bridge.ps1", "Install-Shuttle.ps1",
            "Configure-Shuttle-Mcp.ps1", "Start-Shuttle.ps1", "Stop-Shuttle.ps1",
        ):
            with self.subTest(name=name):
                script = root / name
                self.assertTrue(script.is_file(), f"Missing {name}")
                path = str(script).replace("'", "''")
                command = (
                    "$tokens = $null; $errors = $null; "
                    f"[System.Management.Automation.Language.Parser]::ParseFile('{path}', "
                    "[ref]$tokens, [ref]$errors) | Out-Null; "
                    "if ($errors.Count -gt 0) { "
                    "$errors | ForEach-Object { [Console]::Error.WriteLine($_) }; exit 1 }"
                )
                result = subprocess.run(
                    ["pwsh", "-NoProfile", "-Command", command],
                    capture_output=True, text=True, timeout=15, check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
