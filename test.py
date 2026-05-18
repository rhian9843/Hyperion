# testing script for Hyperion
import unittest
from subprocess import run, PIPE

import tempfile
import os

DATABASE_COMMAND = ["python3", "hyperion.py"]


def get_commands_from_array(command_array):
    return "\n".join([str(x) for x in command_array]) + "\n"


def decompose_output_from_program(output_string):
    return [str(x) for x in output_string.split("\n")]


def run_test_commands(commands):
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        output = run(DATABASE_COMMAND + [db_path], stdout=PIPE, input=commands, encoding="ascii")
        return (output.returncode, output.stdout)
    finally:
        os.unlink(db_path)


def validate_test(command_list, target_output_list):
    # returns a boolean with the status of the commands to be run
    return_code, stdout = run_test_commands(get_commands_from_array(command_list))
    print(stdout, return_code)
    if return_code != 0:
        print(f"Failed with {return_code}")
        return False
    else:
        print("input array")
        print(command_list)
        output_list = decompose_output_from_program(stdout)
        print("output array")
        print(output_list)
        return output_list == target_output_list


class BasicTest(unittest.TestCase):
    # sanity checks
    def test_basic_prompt(self):
        self.assertTrue(validate_test([".exit"], ["H > "]))


class QueryTest(unittest.TestCase):
    def test_single_row_insert(self):
        # runs tests for basic queries
        self.assertTrue(
            validate_test(
                ["insert 1 A abc@amail.com", "select", ".exit"],
                [
                    "H > Executed.",
                    "H > (1, A, abc@amail.com)",
                    "Executed.",
                    "H > ",
                ],
            )
        )

    def test_insertion_error(self):
        # expect the DB to fail when you insert too many rows
        queryset = [f"insert {x} user{x} user{x}@x.com" for x in range(1, 1402)]
        queryset.append(".exit")
        self.assertFalse(validate_test(queryset, []))


if __name__ == "__main__":
    unittest.main()
