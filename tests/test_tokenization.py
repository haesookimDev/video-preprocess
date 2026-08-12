"""Unit tests for the lazy Hugging Face token counter adapter."""

from video_preprocess.tokenization import (
    HuggingFaceTokenCounter,
    sentence_transformer_tokenizer_model,
)


class FakeTokenizer:
    def encode(self, text, *, add_special_tokens):
        assert add_special_tokens is False
        return list(text)

    def decode(
        self,
        token_ids,
        *,
        skip_special_tokens,
        clean_up_tokenization_spaces,
    ):
        assert skip_special_tokens is True
        assert clean_up_tokenization_spaces is False
        return "".join(token_ids)


def test_hugging_face_counter_loads_once_and_truncates_exact_tokens() -> None:
    loaded = []

    def loader(model_name, revision):
        loaded.append((model_name, revision))
        return FakeTokenizer()

    counter = HuggingFaceTokenCounter(
        "target/model",
        revision="rev-1",
        loader=loader,
    )

    assert counter.count("가나다라") == 4
    assert counter.truncate("가나다라", 2) == "가나"
    assert loaded == [("target/model", "rev-1")]


def test_sentence_transformer_short_model_uses_default_organization() -> None:
    assert (
        sentence_transformer_tokenizer_model("paraphrase-multilingual-MiniLM-L12-v2")
        == "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    )
    assert sentence_transformer_tokenizer_model("owner/model") == "owner/model"
