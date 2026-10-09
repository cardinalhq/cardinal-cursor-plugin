"""Completion guidance shared by session adapters; never starts generation.

This is injected before the model responds, not by a blocking Stop hook: the
user can decline or leave, and completing visualization work cannot restart it.
"""

VISUALIZATION_OFFER = 'At the end of your work, before ending your response, ask the user: "Would you like me to update the storyboard visualization?" Wait for an affirmative reply before using the storyboard skill to author, preview and refine this same storyboard. A decline leaves the existing visualization unchanged. Do not offer again on a turn spent only answering the visualization offer, declining, creating, updating, previewing, publishing or sharing the visualization, or when this work already included the requested visualization update. Offer again after subsequent investigation work. Updating visuals does not authorize publishing or sharing.'
