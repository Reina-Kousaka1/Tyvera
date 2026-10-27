from __future__ import annotations

import io
import shutil
import tempfile
import unittest
from pathlib import Path

from tyvera.compiler import compile_project
from tyvera.interpreter import Interpreter


ROOT = Path(__file__).resolve().parents[1]
GATEWAY = ROOT / "libraries" / "discord" / "src" / "gateway.tyv"


class DiscordSourcePackageTests(unittest.TestCase):
    def _run(self, body: str) -> str:
        with tempfile.TemporaryDirectory(prefix="tyvera-discord-") as temporary:
            root = Path(temporary)
            (root / "src").mkdir()
            shutil.copyfile(GATEWAY, root / "src" / "gateway.tyv")
            shutil.copyfile(GATEWAY.with_name("rest.tyv"), root / "src" / "rest.tyv")
            (root / "src" / "main.tyv").write_text(
                'import "gateway.tyv" as gateway\n'
                'import "rest.tyv" as rest\n' + body,
                encoding="utf-8",
            )
            program, _ = compile_project(root / "src" / "main.tyv", root)
            output = io.StringIO()
            Interpreter(program, output, project_root=root).run()
            return output.getvalue()

    def test_gateway_dispatch_decodes_ready_message_and_unknown_events(self) -> None:
        output = self._run(r'''fn main() {
    match gateway.decodeDispatch("{\"t\":\"READY\",\"d\":{\"session_id\":\"s-1\"}}") {
        Ok(event) => match event {
            gateway.GatewayDispatch.Ready(session) => println(session)
            gateway.GatewayDispatch.MessageCreate(_, _, _) => println("wrong")
            gateway.GatewayDispatch.Unknown(_) => println("wrong")
        }
        Err(_) => println("wrong")
    }
    match gateway.decodeDispatch("{\"t\":\"MESSAGE_CREATE\",\"d\":{\"id\":\"m-2\",\"channel_id\":\"c-3\",\"content\":\"hello\"}}") {
        Ok(event) => match event {
            gateway.GatewayDispatch.Ready(_) => println("wrong")
            gateway.GatewayDispatch.MessageCreate(id, channel, content) => {
                println(id)
                println(channel)
                match content {
                    Some(text) => println(text)
                    None => println("no content")
                }
            }
            gateway.GatewayDispatch.Unknown(_) => println("wrong")
        }
        Err(_) => println("wrong")
    }
    match gateway.decodeDispatch("{\"t\":\"PRESENCE_UPDATE\",\"d\":{}}") {
        Ok(event) => match event {
            gateway.GatewayDispatch.Ready(_) => println("wrong")
            gateway.GatewayDispatch.MessageCreate(_, _, _) => println("wrong")
            gateway.GatewayDispatch.Unknown(name) => println(name)
        }
        Err(_) => println("wrong")
    }
    match gateway.decodeDispatch("{\"t\":\"MESSAGE_CREATE\",\"d\":{\"id\":\"m-3\",\"channel_id\":\"c-3\"}}") {
        Ok(event) => match event {
            gateway.GatewayDispatch.MessageCreate(_, _, content) => match content {
                Some(_) => println("wrong")
                None => println("content omitted")
            }
            gateway.GatewayDispatch.Ready(_) => println("wrong")
            gateway.GatewayDispatch.Unknown(_) => println("wrong")
        }
        Err(_) => println("wrong")
    }
}''')
        self.assertEqual(output, "s-1\nm-2\nc-3\nhello\nPRESENCE_UPDATE\ncontent omitted\n")

    def test_gateway_dispatch_errors_are_result_values_without_echoing_payload(self) -> None:
        output = self._run(r'''fn main() {
    match gateway.decodeDispatch("not-json") {
        Ok(_) => println("wrong")
        Err(error) => println(error)
    }
    match gateway.decodeDispatch("{\"t\":4,\"d\":{}}") {
        Ok(_) => println("wrong")
        Err(error) => println(error)
    }
    match gateway.decodeDispatch("{\"t\":\"READY\",\"d\":{}}") {
        Ok(_) => println("wrong")
        Err(error) => println(error)
    }
    match gateway.decodeDispatch("{\"t\":\"UNKNOWN\"}") {
        Ok(_) => println("wrong")
        Err(error) => println(error)
    }
    match gateway.decodeDispatch("{\"t\":\"MESSAGE_CREATE\",\"d\":{\"id\":\"m-4\",\"channel_id\":\"c-4\",\"content\":4}}") {
        Ok(_) => println("wrong")
        Err(error) => println(error)
    }
}''')
        self.assertEqual(
            output,
            "invalid gateway JSON payload\n"
            "gateway field must be a string\n"
            "gateway field is missing\n"
            "gateway dispatch data is missing\n"
            "MESSAGE_CREATE content must be a string\n",
        )

    def test_checked_in_decoder_project_checks_and_runs(self) -> None:
        project = ROOT / "libraries" / "discord"
        program, _ = compile_project(project / "examples" / "decode.tyv", project)
        output = io.StringIO()
        Interpreter(program, output, project_root=project).run()
        self.assertEqual(output.getvalue(), "demo-session\n429\n0.5\ntrue\n")

    def test_rest_failure_parser_maps_status_and_rate_limit_metadata(self) -> None:
        output = self._run(r'''fn main() {
    match rest.decodeFailure(400, "{\"code\":50035,\"message\":\"Invalid Form Body\"}") {
        Ok(failure) => {
            println(failure.status)
            match failure.code {
                Some(code) => println(code)
                None => println("no code")
            }
            println(failure.message)
            println(failure.global)
        }
        Err(_) => println("invalid")
    }
    match rest.decodeFailure(429, "{\"message\":\"slow down\",\"retry_after\":1.25,\"global\":true}") {
        Ok(failure) => match failure.retryAfterSeconds {
            Some(retryDelay) => println(retryDelay)
            None => println("missing retry")
        }
        Err(_) => println("invalid")
    }
}''')
        self.assertEqual(
            output,
            "400\n50035\nInvalid Form Body\nfalse\n1.25\n",
        )

    def test_rest_failure_parser_rejects_malformed_429_metadata(self) -> None:
        output = self._run(r'''fn main() {
    match rest.decodeFailure(429, "{\"message\":\"slow down\"}") {
        Ok(_) => println("wrong")
        Err(_) => println("missing retry rejected")
    }
    match rest.decodeFailure(429, "{\"message\":\"slow down\",\"retry_after\":-1.0}") {
        Ok(_) => println("wrong")
        Err(_) => println("negative retry rejected")
    }
    match rest.decodeFailure(200, "not-json") {
        Ok(_) => println("wrong")
        Err(_) => println("bad body rejected")
    }
}''')
        self.assertEqual(
            output,
            "missing retry rejected\nnegative retry rejected\nbad body rejected\n",
        )


if __name__ == "__main__":
    unittest.main()
