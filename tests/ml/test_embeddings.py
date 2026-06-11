# tests/ml/test_embeddings.py
"""Tests for embedding computation and Lead embedding fields."""
from __future__ import annotations

from datetime import date
from unittest.mock import patch, MagicMock

import numpy as np
import pytest


@pytest.mark.no_embed_mock
class TestEmbedText:
    def test_embed_text_returns_384_dim(self):
        mock_model = MagicMock()
        mock_model.embed.return_value = [np.random.randn(384).astype(np.float32)]

        with patch("linkedin.ml.embeddings._model", mock_model):
            from linkedin.ml.embeddings import embed_text
            result = embed_text("hello world")

        assert result.shape == (384,)
        assert result.dtype == np.float32

    def test_embed_texts_returns_batch(self):
        mock_model = MagicMock()
        mock_model.embed.return_value = [
            np.random.randn(384).astype(np.float32),
            np.random.randn(384).astype(np.float32),
        ]

        with patch("linkedin.ml.embeddings._model", mock_model):
            from linkedin.ml.embeddings import embed_texts
            result = embed_texts(["hello", "world"])

        assert result.shape == (2, 384)


class TestLeadEmbeddingFields:
    def test_store_and_retrieve(self, db):
        from crm.models import Lead

        emb = np.random.randn(384).astype(np.float32)
        Lead.objects.create(
            pk=1, public_identifier="alice", linkedin_url="https://linkedin.com/in/alice/",
            embedding=emb.tobytes(),
        )

        lead = Lead.objects.get(pk=1)
        np.testing.assert_array_almost_equal(lead.embedding_array, emb)

    def test_embedding_array_setter(self, db):
        from crm.models import Lead

        emb = np.random.randn(384).astype(np.float32)
        lead = Lead(pk=1, public_identifier="alice", linkedin_url="https://linkedin.com/in/alice/")
        lead.embedding_array = emb
        lead.save()

        lead = Lead.objects.get(pk=1)
        np.testing.assert_array_almost_equal(lead.embedding_array, emb)

    def test_embedding_array_none_when_no_embedding(self, db):
        from crm.models import Lead

        lead = Lead.objects.create(
            pk=1, public_identifier="alice", linkedin_url="https://linkedin.com/in/alice/",
        )
        assert lead.embedding_array is None

    def test_embedded_lead_ids(self, db):
        from crm.models import Lead

        emb = np.random.randn(384).astype(np.float32)
        Lead.objects.create(
            pk=1, public_identifier="alice",
            linkedin_url="https://linkedin.com/in/alice/",
            embedding=emb.tobytes(),
        )
        Lead.objects.create(
            pk=2, public_identifier="bob",
            linkedin_url="https://linkedin.com/in/bob/",
            embedding=emb.tobytes(),
        )

        ids = set(Lead.objects.filter(embedding__isnull=False).values_list("pk", flat=True))
        assert ids == {1, 2}
