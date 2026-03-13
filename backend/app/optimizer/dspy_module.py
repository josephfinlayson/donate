"""DSPy module for the persuasion chatbot.

This wraps the chatbot's behavior as a DSPy module so GEPA
can optimize the system prompt (instructions).
"""

import dspy


class PersuasionChatbot(dspy.Module):
    """DSPy module for the charity persuasion chatbot.

    The signature's instructions are what GEPA optimizes.
    The conversation_history and user_message are inputs,
    and bot_response is the output.
    """

    def __init__(self):
        self.persuade = dspy.ChainOfThought(
            "conversation_history, user_message -> bot_response"
        )

    def forward(self, conversation_history: str, user_message: str):
        return self.persuade(
            conversation_history=conversation_history,
            user_message=user_message,
        )
