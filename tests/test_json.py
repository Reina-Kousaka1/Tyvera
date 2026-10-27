from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path

from tyvera.compiler import compile_source
from tyvera.interpreter import Interpreter
from tyvera.json_limits import MAX_JSON_DOCUMENT_BYTES, MAX_JSON_NESTING
from tyvera.source import Diagnostic


class JsonTests(unittest.TestCase):
    def _run(self, source: str) -> str:
        program, _ = compile_source(source, "json.tyv")
        with tempfile.TemporaryDirectory() as temporary:
            output = io.StringIO()
            Interpreter(program, output, project_root=Path(temporary)).run()
            return output.getvalue()

    def test_parse_accessors_patterns_and_stringify(self) -> None:
        output = self._run(r'''fn main() {
    match parseJson("{\"name\":\"Ada\",\"scores\":[3,5],\"active\":true,\"nil\":null}") {
        Ok(root) => {
            println(root.kind())
            match root.objectGet("name") {
                Some(name) => match name.asString() {
                    Some(value) => println(value)
                    None => println("wrong name type")
                }
                None => println("missing name")
            }
            match root.objectGet("scores") {
                Some(scores) => {
                    match scores.arrayLength() {
                        Some(length) => println(length)
                        None => println("not an array")
                    }
                    match scores.arrayAt(1) {
                        Some(score) => match score.asInt() {
                            Some(value) => println(value)
                            None => println("wrong score type")
                        }
                        None => println("missing score")
                    }
                }
                None => println("missing scores")
            }
            match root.objectGet("nil") {
                Some(value) => println(value.isNull())
                None => println("missing nil")
            }
            match root.objectKeys() {
                Some(keys) => for key in keys { println(key) }
                None => println("not an object")
            }
            match jsonStringify(root) {
                Ok(text) => println(text)
                Err(_) => println("encoding failed")
            }
        }
        Err(_) => println("parse failed")
    }
}''')
        self.assertEqual(output, "object\nAda\n2\n5\ntrue\nname\nscores\nactive\nnil\n{\"name\":\"Ada\",\"scores\":[3,5],\"active\":true,\"nil\":null}\n")

    def test_parse_scalar_values_and_float_kinds(self) -> None:
        self.assertEqual(self._run(r'''fn main() {
    match parseJson("[\"x\",-2,1.5,false,null]") {
        Ok(array) => {
            match array.arrayAt(0) {
                Some(value) => println(value.kind())
                None => {}
            }
            match array.arrayAt(1) {
                Some(value) => println(value.kind())
                None => {}
            }
            match array.arrayAt(2) {
                Some(value) => println(value.kind())
                None => {}
            }
            match array.arrayAt(3) {
                Some(value) => println(value.kind())
                None => {}
            }
            match array.arrayAt(4) {
                Some(value) => println(value.kind())
                None => {}
            }
        }
        Err(_) => println("parse failed")
    }
}'''), "string\nint\nfloat\nbool\nnull\n")

    def test_json_constructors_encode_typed_collections(self) -> None:
        self.assertEqual(self._run(r'''fn main() {
    let array: List<Json> = List<Json>()
    array.append(jsonString("hello"))
    array.append(jsonBool(true))
    let object: Map<String, Json> = Map<String, Json>()
    object.put("items", jsonArray(array))
    object.put("count", jsonInt(2))
    match jsonStringify(jsonObject(object)) {
        Ok(text) => println(text)
        Err(_) => println("encoding failed")
    }
}'''), '{"items":["hello",true],"count":2}\n')

    def test_parse_errors_are_result_values_without_raw_input(self) -> None:
        output = self._run(r'''fn main() {
    match parseJson("{\"x\":1,\"x\":2}") {
        Ok(_) => println("unexpected")
        Err(message) => println(message)
    }
    match parseJson("9223372036854775808") {
        Ok(_) => println("unexpected")
        Err(message) => println(message)
    }
    match parseJson("not-json") {
        Ok(_) => println("unexpected")
        Err(message) => println(message)
    }
}''')
        self.assertEqual(output, "invalid JSON document\ninvalid JSON document\ninvalid JSON document\n")

    def test_parse_rejects_nonstandard_constants_and_non_finite_numbers(self) -> None:
        self.assertEqual(self._run(r'''fn main() {
    match parseJson("NaN") {
        Ok(_) => println("bad")
        Err(_) => println("rejected")
    }
    match parseJson("1e9999") {
        Ok(_) => println("bad")
        Err(_) => println("rejected")
    }
}'''), "rejected\nrejected\n")

    def test_parse_rejects_unpaired_unicode_surrogates(self) -> None:
        self.assertEqual(self._run(r'''fn main() {
    match parseJson("\"\\ud800\"") {
        Ok(_) => println("bad")
        Err(_) => println("rejected")
    }
}'''), "rejected\n")

    def test_json_document_nesting_limit_is_shared_and_inclusive(self) -> None:
        for depth, expected in ((MAX_JSON_NESTING, "accepted\n"), (MAX_JSON_NESTING + 1, "rejected\n")):
            document = "[" * depth + "0" + "]" * depth
            source = f'''fn main() {{
    match parseJson("{document}") {{
        Ok(_) => println("accepted")
        Err(_) => println("rejected")
    }}
}}'''
            with self.subTest(depth=depth):
                self.assertEqual(self._run(source), expected)

    def test_json_document_byte_limit_returns_a_result_error(self) -> None:
        result = Interpreter._parse_json(" " * (MAX_JSON_DOCUMENT_BYTES + 1))
        self.assertEqual(result.variant, "Err")
        self.assertEqual(result.payload, "invalid JSON document")

    def test_accessors_return_none_for_missing_values_and_wrong_kinds(self) -> None:
        self.assertEqual(self._run(r'''fn main() {
    let value: Json = jsonString("text")
    match value.arrayLength() {
        Some(_) => println("bad")
        None => println("not an array")
    }
    match value.arrayAt(0) {
        Some(_) => println("bad")
        None => println("not an array")
    }
    match value.objectGet("missing") {
        Some(_) => println("bad")
        None => println("not an object")
    }
    match value.objectKeys() {
        Some(_) => println("bad")
        None => println("not an object")
    }
}'''), "not an array\nnot an array\nnot an object\nnot an object\n")

    def test_json_calls_and_accessors_are_statically_typed(self) -> None:
        for source, message in [
            ('fn main() { parseJson(1) }', "expects String, found Int"),
            ('fn main() { jsonInt("1") }', "expects Int, found String"),
            ('fn main() {\n let value: Json = jsonNull()\n value.objectGet(1)\n}', "expects String, found Int"),
            ('fn main() {\n let value: Json = jsonNull()\n value.arrayAt(false)\n}', "expects Int, found Bool"),
            ('fn main() {\n let values: List<Int> = List<Int>()\n jsonArray(values)\n}', "expects List<Json>, found List<Int>"),
        ]:
            with self.subTest(message=message), self.assertRaisesRegex(Diagnostic, message):
                compile_source(source, "bad-json.tyv")

    def test_json_is_a_reserved_type_and_builtin(self) -> None:
        for source in [
            "class Json {} fn main() {}",
            "fn parseJson(value: String) {} fn main() {}",
        ]:
            with self.subTest(source=source), self.assertRaises(Diagnostic):
                compile_source(source, "reserved-json.tyv")


if __name__ == "__main__":
    unittest.main()
