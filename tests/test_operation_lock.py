import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from operation_lock import OperationBusy, OperationLock


class LockTests(unittest.TestCase):
    def test_lock_is_shared_across_data_directories(self):
        with tempfile.TemporaryDirectory() as temp:
            with OperationLock(temp, "request-1"):
                with self.assertRaises(OperationBusy):
                    with OperationLock(temp, "request-2"):
                        pass
            with OperationLock(temp, "request-3"):
                self.assertTrue((Path(temp) / "migrations" / "operation.lock").exists())


if __name__ == "__main__":
    unittest.main()
