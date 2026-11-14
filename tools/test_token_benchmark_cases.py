"""Offline checks for benchmark data and its objective grader."""

import copy
import json
import unittest

from token_benchmark_cases import get_cases, score_case


class CaseTests(unittest.TestCase):
    def test_cases_repeat_and_vary(self):
        cases = get_cases(0, 73)
        self.assertEqual(cases, get_cases(0, 73))
        self.assertNotEqual(cases, get_cases(1, 73))
        self.assertNotEqual(cases, get_cases(0, 74))
        self.assertEqual(len(cases), 8)
        self.assertEqual(len({case["id"] for case in cases}), 8)
        self.assertTrue(all(512 <= case["max_tokens"] <= 4096 for case in cases))

    def test_all_answers_grade_perfectly_across_rounds(self):
        for round_index in range(10):
            for case in get_cases(round_index, 73):
                with self.subTest(round=round_index, case=case["id"]):
                    result = score_case(case, json.dumps(case["expected"], ensure_ascii=False))
                    self.assertEqual(result["score"], 1.0)
                    self.assertTrue(result["passed"])

    def test_long_context_and_sustained_output_have_meaningful_lengths(self):
        cases = {case["id"]: case for case in get_cases(0, 73)}
        self.assertGreaterEqual(len(cases["long_context"]["prompt"]), 4000)
        self.assertLess(len(cases["long_context"]["prompt"]), 7000)
        self.assertEqual(len(cases["sustained_output"]["expected"]["rows"]), 80)

    def test_invalid_formats_and_duplicate_keys_are_rejected(self):
        case = get_cases(0, 73)[0]
        for text in ("not json", "{} extra", "```json\n{}\n```", "[]",
                     '{"x":1,"x":2}', '{"x":NaN}', '{"x":Infinity}',
                     '{"x":1e9999}'):
            with self.subTest(text=text):
                result = score_case(case, text)
                self.assertEqual(result["score"], 0.0)
                self.assertFalse(result["passed"])
                self.assertFalse(result["details"]["format_valid"])

    def test_incorrect_field_has_partial_credit(self):
        case = get_cases(0, 73)[0]
        answer = copy.deepcopy(case["expected"])
        answer["payable_cents"] += 1
        result = score_case(case, json.dumps(answer))
        self.assertEqual(result["score"], 0.8)
        self.assertFalse(result["passed"])
        self.assertTrue(result["details"]["schema_valid"])

    def test_extra_keys_and_missing_items_are_penalized(self):
        case = get_cases(0, 73)[-1]
        answer = copy.deepcopy(case["expected"])
        answer["extra"] = True
        result = score_case(case, json.dumps(answer))
        self.assertEqual(result["score"], 0.9)
        self.assertFalse(result["passed"])
        self.assertFalse(result["details"]["schema_valid"])
        answer = copy.deepcopy(case["expected"])
        answer["rows"].pop()
        result = score_case(case, json.dumps(answer))
        self.assertEqual(result["details"]["matched"], 79 * 3)
        self.assertLess(result["score"], 1.0)

    def test_json_types_and_list_order_are_significant(self):
        case = get_cases(0, 73)[0]
        answer = copy.deepcopy(case["expected"])
        answer["subtotal_cents"] = float(answer["subtotal_cents"])
        self.assertFalse(score_case(case, json.dumps(answer))["passed"])
        case = get_cases(0, 73)[1]
        answer = {"order": list(reversed(case["expected"]["order"]))}
        self.assertEqual(score_case(case, json.dumps(answer))["score"], 0.0)

    def test_whitespace_and_object_key_order_do_not_matter(self):
        case = get_cases(0, 73)[0]
        answer = dict(reversed(list(case["expected"].items())))
        self.assertTrue(score_case(case, " \n" + json.dumps(answer, indent=2) + "\n")["passed"])

    def test_invalid_generation_arguments(self):
        for index, seed in ((-1, 1), (True, 1), (1.0, 1), (0, "x")):
            with self.assertRaises(ValueError):
                get_cases(index, seed)


if __name__ == "__main__":
    unittest.main()
