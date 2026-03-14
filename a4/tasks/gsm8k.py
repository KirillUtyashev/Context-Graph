"""
GSM8K evaluation.
https://huggingface.co/datasets/openai/gsm8k

Example problem instance:

Question:
Weng earns $12 an hour for babysitting. Yesterday, she just did 50 minutes of babysitting. How much did she earn?
Answer:
Weng earns 12/60 = $<<12/60=0.2>>0.2 per minute.
Working 50 minutes, she earned 0.2 x 50 = $<<0.2*50=10>>10.
#### 10

Notice that GSM8K uses tool calls inside << >> tags.
"""

import re
from datasets import load_dataset
from tasks.common import Task


GSM_RE = re.compile(r"#### (\-?[0-9\.\,]+)")
NUMBER_RE = re.compile(r"\-?[0-9][0-9,]*(?:\.[0-9]+)?")
def extract_answer(completion):
    """
    Extract the numerical answer after #### marker.
    Follows official code for normalization:
    https://github.com/openai/grade-school-math/blob/3101c7d5072418e28b9008a6636bde82a006892c/grade_school_math/dataset.py#L28
    """
    match = GSM_RE.search(completion)
    if match:
        match_str = match.group(1).strip()
        match_str = match_str.replace(",", "")
        return match_str
    return None

def extract_last_number(text):
    """Fallback extractor for the last number mentioned anywhere in the completion."""
    matches = NUMBER_RE.findall(text)
    if not matches:
        return None
    return matches[-1].replace(",", "")

def extract_numbers(text):
    """Return normalized numbers mentioned in text, preserving multiplicity."""
    return [match.replace(",", "") for match in NUMBER_RE.findall(text)]


class GSM8K(Task):

    def __init__(self, subset, split, reward_system="exact", **kwargs):
        super().__init__(**kwargs)
        assert subset in ["main", "socratic"], "GSM8K subset must be main|socratic"
        assert split in ["train", "test"], "GSM8K split must be train|test"
        assert reward_system in ["exact", "format", "reasoning", "grounding", "dense", "grounding_dense", "balanced", "grounding_balanced"], f"Unsupported reward system: {reward_system}"
        self.ds = load_dataset("openai/gsm8k", subset, split=split).shuffle(seed=42)
        self.reward_system = reward_system

    @property
    def eval_type(self):
        return 'generative'

    def num_examples(self):
        return len(self.ds)

    def get_example(self, index):
        """ Get a single problem from the dataset. """
        row = self.ds[index]
        question = row['question'] # string of the question prompt
        answer = row['answer'] # string of the full solution and the answer after #### marker
        # Create and return the Conversation object
        # This is tricky because GSM8K uses tool calls, which we need to parse here.
        assistant_message_parts = []
        parts = re.split(r'(<<[^>]+>>)', answer)
        for part in parts:
            if part.startswith('<<') and part.endswith('>>'):
                # This is a calculator tool call
                inner = part[2:-2]  # Remove << >>
                # Split on = to get expression and result
                if '=' in inner:
                    expr, result = inner.rsplit('=', 1)
                else:
                    expr, result = inner, ""
                # Add the tool call as a part
                assistant_message_parts.append({"type": "python", "text": expr})
                # Add the result as a part
                assistant_message_parts.append({"type": "python_output", "text": result})
            else:
                # Regular text in between tool calls
                assistant_message_parts.append({"type": "text", "text": part})
        # Now put it all together
        messages = [
            {"role": "user", "content": question}, # note: simple string
            {"role": "assistant", "content": assistant_message_parts}, # note: list of parts (as dicts)
        ]
        conversation = {
            "messages": messages,
        }
        return conversation

    def evaluate(self, conversation, assistant_response):
        """
        Given (conversation, completion), return evaluation outcome (0 = wrong, 1 = correct)
        Note that:
        - the conversation has both user AND assistant message (containing the ground truth answer)
        - the assistant_response is usually the alternative assistant message achieved via sampling

        TODO: Technically, assistant_response should be a Message (either a string or a list of parts)
              We can handle this later possibly. For now just assume string.
        """
        assert isinstance(assistant_response, str), "Assuming simple string response for now"
        # First extract the ground truth answer
        assistant_message = conversation['messages'][-1]
        assert assistant_message['role'] == "assistant", "Last message must be from the Assistant"
        assert isinstance(assistant_message['content'], list), "This is expected to be a list of parts"
        last_text_part = assistant_message['content'][-1]['text'] # this contains the final answer in GSM8K
        # Extract both the ground truth answer and the predicted answer
        ref_num = extract_answer(last_text_part)
        pred_num = extract_answer(assistant_response)
        # Compare and return the success as int
        is_correct = int(pred_num == ref_num)
        return is_correct

    def reward(self, conversation, assistant_response):
        """
        Used during RL. To keep things simple, just re-use the evaluation above.
        Later this could be made more complex (e.g. format matching etc.)
        """
        assert isinstance(assistant_response, str), "Assuming simple string response for now"
        is_correct = float(self.evaluate(conversation, assistant_response))

        if self.reward_system == "exact":
            return is_correct

        has_canonical_answer = float(extract_answer(assistant_response) is not None)
        has_any_number = float(extract_last_number(assistant_response) is not None)
        nonempty_lines = [line.strip() for line in assistant_response.splitlines() if line.strip()]
        has_multistep_reasoning = float(len(nonempty_lines) >= 2)
        has_arithmetic = float(any(op in assistant_response for op in ["+", "-", "*", "/", "="]))
        word_count = len(assistant_response.split())
        question_text = conversation["messages"][0]["content"]
        question_numbers = set(extract_numbers(question_text))
        response_numbers = set(extract_numbers(assistant_response))
        if question_numbers:
            numeric_grounding = len(question_numbers & response_numbers) / len(question_numbers)
        else:
            numeric_grounding = 0.0
        if 20 <= word_count <= 120:
            length_band_reward = 1.0
        elif 10 <= word_count < 20 or 120 < word_count <= 180:
            length_band_reward = 0.5
        else:
            length_band_reward = 0.0

        if self.reward_system == "format":
            # Dense but aligned: canonical GSM8K answer formatting is rewarded,
            # while exact correctness still dominates.
            return (
                1.0 * is_correct
                + 0.15 * has_canonical_answer
                + 0.10 * has_any_number
            )

        if self.reward_system == "reasoning":
            # Encourage showing intermediate work and ending with a parseable answer,
            # but keep the exact answer as the strongest signal by a wide margin.
            return (
                1.0 * is_correct
                + 0.10 * has_canonical_answer
                + 0.10 * has_multistep_reasoning
                + 0.05 * has_arithmetic
            )

        if self.reward_system == "grounding":
            # Encourage reusing the quantities from the question instead of
            # collapsing to short, generic numeric guesses.
            return (
                1.0 * is_correct
                + 0.10 * has_canonical_answer
                + 0.15 * numeric_grounding
            )

        if self.reward_system == "dense":
            # Combine formatting and reasoning into one denser reward while
            # keeping exact answer correctness dominant.
            return (
                1.0 * is_correct
                + 0.10 * has_canonical_answer
                + 0.10 * has_multistep_reasoning
                + 0.05 * has_arithmetic
            )

        if self.reward_system == "grounding_dense":
            # Add numeric grounding on top of the dense formatting/reasoning
            # reward to discourage short, ungrounded numeric guesses.
            return (
                1.0 * is_correct
                + 0.10 * has_canonical_answer
                + 0.10 * has_multistep_reasoning
                + 0.05 * has_arithmetic
                + 0.15 * numeric_grounding
            )

        if self.reward_system == "balanced":
            # Specifically target the observed RL failure mode: shrinking
            # outputs. Reward solutions that are grounded in the problem and
            # land in a reasonable explanation-length band.
            return (
                1.0 * is_correct
                + 0.10 * has_canonical_answer
                + 0.10 * has_multistep_reasoning
                + 0.10 * numeric_grounding
                + 0.10 * length_band_reward
            )

        if self.reward_system == "grounding_balanced":
            # Balanced reward with stronger numeric grounding emphasis.
            return (
                1.0 * is_correct
                + 0.10 * has_canonical_answer
                + 0.10 * has_multistep_reasoning
                + 0.20 * numeric_grounding
                + 0.10 * length_band_reward
            )

        raise ValueError(f"Unsupported reward system: {self.reward_system}")
