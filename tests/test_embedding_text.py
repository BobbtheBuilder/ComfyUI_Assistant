"""Full input coverage, including text past an embedding context boundary."""
import unittest
from unittest.mock import AsyncMock, patch

import providers
from embedding_text import embedding_parts


class InputCoverageTest(unittest.IsolatedAsyncioTestCase):
    def test_unicode_and_whitespace_are_losslessly_split(self):
        text = ("节点🙂 café\n   " * 100) + "IMPORTANT TAIL"
        parts = embedding_parts(text, 32)
        self.assertEqual("".join(parts), text)
        self.assertTrue(all(len(part.encode("utf-8")) <= 28 for part in parts))
        self.assertTrue(parts[-1].endswith("TAIL"))

    async def test_single_vector_consumers_include_the_tail(self):
        text = "a" * 18 + "TAIL"
        request = AsyncMock(side_effect=[[[1, 0]], [[1, 0]], [[0, 1]]])
        with patch.object(providers, "embedding_limits", AsyncMock(return_value=(10, 36, "model"))), \
                patch.object(providers, "_embed_request", request):
            vectors = await providers.embed({}, [text], "model")
        self.assertEqual("".join(call.args[1][0] for call in request.await_args_list), text)
        self.assertGreater(vectors[0][1], 0)

    async def test_fragment_failure_does_not_return_partial_vector(self):
        with patch.object(providers, "embedding_limits", AsyncMock(return_value=(10, 36, "model"))), \
                patch.object(providers, "_embed_request", AsyncMock(side_effect=[[[1, 0]], []])):
            with self.assertRaisesRegex(ValueError, "incomplete"):
                await providers.embed({}, ["a" * 20], "model")
