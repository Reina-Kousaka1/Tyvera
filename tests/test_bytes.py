from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tyvera.compiler import compile_source
from tyvera.interpreter import Interpreter
from tyvera.source import Diagnostic


class ByteAndBinaryIOTests(unittest.TestCase):
    def _run(self, source: str, root: Path) -> tuple[str, str]:
        program, _ = compile_source(source, "bytes.tyv")
        output, errors = io.StringIO(), io.StringIO()
        Interpreter(program, output, project_root=root, error_output=errors).run()
        return output.getvalue(), errors.getvalue()

    def test_byte_range_conversion_and_value_equality(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output, _ = self._run('''fn main() {
    match byteFromInt(0) {
        Ok(value) => println(value)
        Err(_) => println("bad")
    }
    match byteFromInt(1) {
        Ok(value) => println(value.toInt())
        Err(_) => println("bad")
    }
    match byteFromInt(254) {
        Ok(value) => println(value.toInt())
        Err(_) => println("bad")
    }
    match byteFromInt(255) {
        Ok(value) => println(value)
        Err(_) => println("bad")
    }
    match byteFromInt(-1) {
        Ok(_) => println("bad")
        Err(_) => println("negative rejected")
    }
    match byteFromInt(256) {
        Ok(_) => println("bad")
        Err(_) => println("overflow rejected")
    }
    match byteFromInt(255) {
        Ok(first) => match byteFromInt(255) {
            Ok(second) => println(first == second)
            Err(_) => println(false)
        }
        Err(_) => println(false)
    }
}
''', Path(temporary))
            self.assertEqual(output, "0\n1\n254\n255\nnegative rejected\noverflow rejected\ntrue\n")

    def test_byte_is_a_stable_map_key_and_transferable_actor_message(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output, _ = self._run('''actor Collector {
    on receive(value: Byte) { println(value) }
}
fn main() {
    let values: Map<Byte, String> = Map<Byte, String>()
    let collector = spawn(Collector())
    match byteFromInt(255) {
        Ok(value) => {
            values.put(value, "maximum")
            match values.get(value) {
                Some(text) => println(text)
                None => println("missing")
            }
            collector.receive(value)
        }
        Err(_) => println("conversion failed")
    }
    runActors()
}
''', Path(temporary))
            self.assertEqual(output, "maximum\n255\n")

    def test_byte_has_no_implicit_integer_or_string_conversion(self) -> None:
        cases = [
            ('fn main() { let value: Byte = 255 }', "expected Byte, found Int"),
            ('''fn main() {
    match byteFromInt(255) {
        Ok(value) => { let text: String = value }
        Err(_) => {}
    }
}''', "expected String, found Byte"),
            ('fn main() { let value: Int = byteFromInt(1) }', "expected Int, found Result<Byte,String>"),
            ('fn main() { let value: Bytes = "text" }', "expected Bytes, found String"),
            ('fn main() { byteFromInt("255") }', "expects Int, found String"),
            ('''fn main() {
    let value = byteFromInt(1)
    println(value == 1)
}''', "cannot compare Result<Byte,String> with Int"),
            ('''fn main() {
    match byteFromInt(1) {
        Ok(value) => { let number: Int = value }
        Err(_) => {}
    }
}''', "expected Int, found Byte"),
        ]
        for source, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(Diagnostic, message):
                compile_source(source, "bad-byte.tyv")

    def test_mutable_byte_buffer_zero_fills_checks_every_index_and_snapshots(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output, _ = self._run('''fn main() {
    match byteBuffer(0) {
        Ok(empty) => {
            println(empty.length())
            match empty.get(0) {
                Ok(_) => println("bad")
                Err(_) => println("empty bounds")
            }
        }
        Err(_) => println("bad allocation")
    }
    match byteBuffer(1) {
        Ok(single) => match single.get(0) {
            Ok(value) => println(value)
            Err(_) => println("single-byte bounds")
        }
        Err(_) => println("bad allocation")
    }
    match byteBuffer(4) {
        Ok(buffer) => {
            println(buffer.length())
            match buffer.get(0) {
                Ok(value) => println(value)
                Err(_) => println("bad first")
            }
            match byteFromInt(255) {
                Ok(maximum) => {
                    match byteFromInt(1) {
                        Ok(middle) => {
                            match buffer.set(1, middle) {
                                Ok(_) => match buffer.get(1) {
                                    Ok(value) => println(value)
                                    Err(_) => println("bad middle")
                                }
                                Err(_) => println("bad middle write")
                            }
                        }
                        Err(_) => println("bad byte")
                    }
                    match buffer.set(3, maximum) {
                        Ok(wrote) => println(wrote)
                        Err(_) => println("bad write")
                    }
                    match buffer.get(3) {
                        Ok(value) => println(value)
                        Err(_) => println("bad last")
                    }
                    match buffer.set(-1, maximum) {
                        Ok(_) => println("bad")
                        Err(_) => println("negative bounds")
                    }
                    match buffer.set(4, maximum) {
                        Ok(_) => println("bad")
                        Err(_) => println("length bounds")
                    }
                    match buffer.get(5) {
                        Ok(_) => println("bad")
                        Err(_) => println("upper bounds")
                    }
                }
                Err(_) => println("bad byte")
            }
            match buffer.toBytes() {
                Err(_) => println("snapshot allocation failed")
                Ok(snapshot) => {
                    println(snapshot.length())
                    match snapshot.get(3) {
                        Ok(value) => println(value)
                        Err(_) => println("bad snapshot")
                    }
                    match snapshot.get(-1) {
                        Ok(_) => println("bad")
                        Err(_) => println("bytes bounds")
                    }
                    match snapshot.get(4) {
                        Ok(_) => println("bad")
                        Err(_) => println("bytes length bounds")
                    }
                    match snapshot.get(5) {
                        Ok(_) => println("bad")
                        Err(_) => println("bytes upper bounds")
                    }
                    match snapshot.slice(1, 2) {
                        Ok(part) => println(part.length())
                        Err(_) => println("bad slice")
                    }
                    match snapshot.slice(3, 2) {
                        Ok(_) => println("bad")
                        Err(_) => println("slice bounds")
                    }
                }
            }
        }
        Err(_) => println("bad allocation")
    }
    match byteBuffer(-1) {
        Ok(_) => println("bad")
        Err(_) => println("negative length rejected")
    }
    match byteBuffer(67108865) {
        Ok(_) => println("bad")
        Err(_) => println("oversize rejected")
    }
}
''', Path(temporary))
            self.assertEqual(output, (
                "0\nempty bounds\n0\n4\n0\n1\ntrue\n255\nnegative bounds\nlength bounds\nupper bounds\n"
                "4\n255\nbytes bounds\nbytes length bounds\nbytes upper bounds\n2\nslice bounds\n"
                "negative length rejected\noversize rejected\n"
            ))

    def test_buffer_operations_are_statically_typed(self) -> None:
        for source, message in [
            ('fn main() { byteBuffer("2") }', "expects Int, found String"),
            ('''fn main() {
    match byteBuffer(1) {
        Ok(buffer) => { buffer.set(0, "x") }
        Err(_) => {}
    }
}''', "expects Byte, found String"),
            ('''fn main() {
    match bytesFromString("x").get("0") {
        Ok(_) => {}
        Err(_) => {}
    }
}''', "expects Int, found String"),
            ('fn main() { readBytes(1) }', "expects String, found Int"),
            ('fn main() { writeBytes("x", "bytes") }', "expects Bytes, found String"),
        ]:
            with self.subTest(message=message), self.assertRaises(Diagnostic) as caught:
                compile_source(source, "bad-buffer.tyv")
            self.assertIn(message, caught.exception.message)

    def test_binary_io_round_trips_empty_nul_and_non_utf8_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output, _ = self._run('''fn main() {
    match byteBuffer(3) {
        Ok(buffer) => match byteFromInt(255) {
            Ok(maximum) => {
                buffer.set(0, maximum)
                match byteFromInt(0) {
                    Ok(zero) => {
                        buffer.set(1, zero)
                        match byteFromInt(1) {
                            Ok(one) => {
                                buffer.set(2, one)
                                match buffer.toBytes() {
                                    Err(_) => println("snapshot failed")
                                    Ok(payload) => match writeBytes("binary.dat", payload) {
                                        Ok(_) => match readBytes("binary.dat") {
                                            Ok(bytes) => {
                                                println(bytes.length())
                                                match bytes.get(0) {
                                                    Ok(value) => println(value)
                                                    Err(_) => println("bad")
                                                }
                                                match bytes.get(1) {
                                                    Ok(value) => println(value)
                                                    Err(_) => println("bad")
                                                }
                                                match bytes.get(2) {
                                                    Ok(value) => println(value)
                                                    Err(_) => println("bad")
                                                }
                                                match bytes.toString() {
                                                    Ok(_) => println("bad utf8")
                                                    Err(_) => println("binary stays binary")
                                                }
                                            }
                                            Err(_) => println("read failed")
                                        }
                                        Err(_) => println("write failed")
                                    }
                                }
                            }
                            Err(_) => println("bad byte")
                        }
                    }
                    Err(_) => println("bad byte")
                }
            }
            Err(_) => println("bad byte")
        }
        Err(_) => println("bad allocation")
    }
    match writeBytes("empty.dat", bytesFromString("")) {
        Ok(_) => {
            match readBytes("empty.dat") {
                Ok(bytes) => println(bytes.length())
                Err(_) => println("empty read failed")
            }
        }
        Err(_) => println("empty write failed")
    }
    match readBytes("missing.dat") {
        Ok(_) => println("bad")
        Err(_) => println("missing handled")
    }
    match readBytes(".") {
        Ok(_) => println("bad")
        Err(_) => println("read error handled")
    }
    match writeBytes(".", bytesFromString("x")) {
        Ok(_) => println("bad")
        Err(_) => println("write error handled")
    }
    match readBytes(".tyvera.secrets") {
        Ok(_) => println("bad")
        Err(_) => println("secret protected")
    }
    match writeBytes(".tyvera.secrets", bytesFromString("x")) {
        Ok(_) => println("bad")
        Err(_) => println("secret protected")
    }
}
''', root)
            self.assertEqual(output, "3\n255\n0\n1\nbinary stays binary\n0\nmissing handled\nread error handled\nwrite error handled\nsecret protected\nsecret protected\n")
            self.assertEqual((root / "binary.dat").read_bytes(), b"\xff\x00\x01")
            self.assertEqual((root / "empty.dat").read_bytes(), b"")

    def test_binary_io_reads_large_payload_and_normalizes_permission_errors(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            payload = bytes(range(256)) * 8192
            (root / "large.dat").write_bytes(payload)
            output, _ = self._run('''fn main() {
    match readBytes("large.dat") {
        Ok(bytes) => {
            println(bytes.length())
            match bytes.get(255) {
                Ok(value) => println(value)
                Err(_) => println("bad")
            }
        }
        Err(_) => println("read failed")
    }
    match writeBytes("target.dat", bytesFromString("data")) {
        Ok(_) => println("write succeeded")
        Err(_) => println("write denied")
    }
}
''', root)
            self.assertEqual(output, "2097152\n255\nwrite succeeded\n")

            program, _ = compile_source('''fn main() {
    match readBytes("large.dat") {
        Ok(_) => println("unexpected")
        Err(_) => println("permission error handled")
    }
}''', "permission.tyv")
            stdout, errors = io.StringIO(), io.StringIO()
            with patch.object(Path, "open", side_effect=PermissionError("denied")):
                Interpreter(program, stdout, project_root=root, error_output=errors).run()
            self.assertEqual(stdout.getvalue(), "permission error handled\n")
            self.assertEqual(errors.getvalue(), "")

            write_program, _ = compile_source('''fn main() {
    match writeBytes("denied.dat", bytesFromString("data")) {
        Ok(_) => println("unexpected")
        Err(_) => println("write permission error handled")
    }
}''', "write-permission.tyv")
            stdout, errors = io.StringIO(), io.StringIO()
            with patch.object(Path, "write_bytes", side_effect=PermissionError("denied")):
                Interpreter(write_program, stdout, project_root=root, error_output=errors).run()
            self.assertEqual(stdout.getvalue(), "write permission error handled\n")
            self.assertEqual(errors.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
