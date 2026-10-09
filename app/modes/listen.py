"""Listening: hear the name, pick the face.

The same question as reverse, asked the way a room asks it — someone says a name and you have
to find the face. Speech is produced by the browser (`speechSynthesis`), so nothing is
downloaded, no voice is bundled, and the app stays dependency-free; where a browser cannot
speak, the name is shown on request instead and the question is still answerable.

`speak` is the one payload field that carries the answer's *prompt* rather than its answer —
the name is the question here, so it is allowed to be in the page. The faces never carry names.
"""

from __future__ import annotations

from app.modes.base import BuildContext
from app.modes.reverse import Reverse


class Listen(Reverse):
    key = "listen"
    label_key = "mode.listen.label"
    prompt_key = "mode.listen.prompt"
    direction = "name_to_face"
    demand = 4            # one rung above reverse: no written name to read

    def build(self, ctx: BuildContext) -> dict:
        payload = super().build(ctx)
        name = str(ctx.target["display_name"])
        payload["prompt"] = self.prompt
        payload["speak"] = name
        payload["name"] = name
        return payload
