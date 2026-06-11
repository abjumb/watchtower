from watchtower.model_api import ModelApiConfig, _extract_text


def test_model_api_config_detects_provider_keys() -> None:
    config = ModelApiConfig(openai_api_key="openai-key")

    assert config.has_key("openai")
    assert not config.has_key("anthropic")
    assert not config.has_key("local")


def test_extract_text_handles_nested_provider_payloads() -> None:
    payload = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {"text": "hello"},
                        {"text": "world"},
                    ]
                }
            }
        ]
    }

    assert _extract_text(payload) == "hello\nworld"
