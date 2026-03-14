"""
NuminaMath-CoT by AI-MO.
https://huggingface.co/datasets/AI-MO/NuminaMath-CoT
"""

from datasets import load_dataset
from tasks.common import Task


class NuminaMathCoT(Task):
    """NuminaMath chain-of-thought data with chat-formatted `messages`."""

    def __init__(self, split, **kwargs):
        super().__init__(**kwargs)
        assert split in ["train", "test"], "NuminaMath split must be train|test"
        self.ds = load_dataset("AI-MO/NuminaMath-CoT", split=split).shuffle(seed=42)
        self.length = len(self.ds)

    def num_examples(self):
        return self.length

    def get_example(self, index):
        row = self.ds[index]
        messages = row["messages"]
        assert len(messages) >= 2, "NuminaMath messages must have at least 2 turns"
        for i, message in enumerate(messages):
            expected_role = "user" if i % 2 == 0 else "assistant"
            assert message["role"] == expected_role, f"Message {i} has role {message['role']} but should be {expected_role}"
            assert isinstance(message["content"], str), "Content must be a string"
        return {"messages": messages}