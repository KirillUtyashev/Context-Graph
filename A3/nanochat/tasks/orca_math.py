"""
Orca-Math word problems by Hugging Face H4.
https://huggingface.co/datasets/HuggingFaceH4/orca-math-word-problems-200k
"""

from datasets import load_dataset
from tasks.common import Task


class OrcaMath(Task):
    """Orca-Math SFT data with chat-formatted `messages`."""

    SPLIT_MAP = {
        "train": "train_sft",
        "test": "test_sft",
        "train_sft": "train_sft",
        "test_sft": "test_sft",
    }

    def __init__(self, split, **kwargs):
        super().__init__(**kwargs)
        assert split in self.SPLIT_MAP, f"split must be one of {tuple(self.SPLIT_MAP)}"
        self.split = self.SPLIT_MAP[split]
        self.ds = load_dataset("HuggingFaceH4/orca-math-word-problems-200k", split=self.split).shuffle(seed=42)
        self.length = len(self.ds)

    def num_examples(self):
        return self.length

    def get_example(self, index):
        row = self.ds[index]
        messages = row["messages"]
        assert len(messages) >= 2, "Orca-Math messages must have at least 2 turns"
        for i, message in enumerate(messages):
            expected_role = "user" if i % 2 == 0 else "assistant"
            assert message["role"] == expected_role, f"Message {i} has role {message['role']} but should be {expected_role}"
            assert isinstance(message["content"], str), "Content must be a string"
        return {"messages": messages}
