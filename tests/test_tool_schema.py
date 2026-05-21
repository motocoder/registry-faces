"""Tool schema introspection — JSON Schema derived from Python signatures."""

from registry_faces.agent.tool_schema import tool_schema, tool_to_openai_schema


def test_simple_signature():
    def head_url(url: str) -> str:
        """Get URL metadata.

        Args:
            url: The URL to inspect.
        """
        return ""

    s = tool_schema(head_url)
    assert s["name"] == "head_url"
    assert s["description"].startswith("Get URL metadata")
    assert s["parameters"]["required"] == ["url"]
    assert s["parameters"]["properties"]["url"]["type"] == "string"
    assert s["parameters"]["properties"]["url"]["description"] == "The URL to inspect."


def test_default_parameter_not_required():
    def fetch(url: str, max_bytes: int = 40000) -> str:
        """Fetch text.

        Args:
            url: target.
            max_bytes: size cap.
        """
        return ""

    s = tool_schema(fetch)
    assert s["parameters"]["required"] == ["url"]
    assert "max_bytes" in s["parameters"]["properties"]
    assert s["parameters"]["properties"]["max_bytes"]["type"] == "integer"


def test_optional_type_via_union():
    def f(name: str | None = None) -> str:
        """Do a thing.

        Args:
            name: optional name.
        """
        return ""

    s = tool_schema(f)
    # Optional unwraps to the non-None type's JSON type
    assert s["parameters"]["properties"]["name"]["type"] == "string"


def test_bool_param():
    def f(flag: bool) -> str:
        """Toggle.

        Args:
            flag: on/off.
        """
        return ""

    assert tool_schema(f)["parameters"]["properties"]["flag"]["type"] == "boolean"


def test_openai_wrapper_shape():
    def f(x: int) -> str:
        """Doc.

        Args:
            x: a number.
        """
        return ""

    s = tool_to_openai_schema(f)
    assert s["type"] == "function"
    assert s["function"]["name"] == "f"
    assert s["function"]["parameters"]["properties"]["x"]["type"] == "integer"


def test_no_args_section_still_works():
    def ping() -> str:
        """No arguments."""
        return ""

    s = tool_schema(ping)
    assert s["description"] == "No arguments."
    assert s["parameters"]["required"] == []
    assert s["parameters"]["properties"] == {}
