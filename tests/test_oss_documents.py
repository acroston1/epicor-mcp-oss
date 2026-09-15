"""Local document substring fallback and portable indexing CLIs."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import pytest


def test_document_ingestion_and_case_insensitive_substring_without_embeddings(tmp_path):
    from epicor_mcp.index.docs_index import DocsIndex
    docs = tmp_path / 'documents'
    docs.mkdir()
    (docs / 'guide.md').write_text('# Guide\nSynthetic purchasing workflow uses APPROVALCHECK before posting.\n')
    output = tmp_path / 'docs.db'
    result = subprocess.run([sys.executable, 'scripts/build_docs_index.py', '--input-dir', str(docs), '--output', str(output)], text=True, capture_output=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert output.is_file()
    with DocsIndex(output) as index:
        matches = index.search('provalcheck')
        assert matches, 'substring inside a word must work without vector indexing'
        assert 'APPROVALCHECK' in matches[0]['content']
        assert index.search('PROVALCHECK') == matches
        assert index.search('missing-token') == []
        assert index.search('') == []
        assert index.search('%') == [], 'LIKE wildcards must be treated literally'
    assert not list(tmp_path.rglob('*.faiss'))


def test_optional_document_embedding_script_exposes_model_choice():
    result = subprocess.run([sys.executable, 'scripts/build_document_vectors.py', '--help'], text=True, capture_output=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert '--model' in result.stdout
    assert '--database' in result.stdout
    assert '--output' in result.stdout


def test_schema_bootstrap_help_works_without_tenant_schema():
    result = subprocess.run([sys.executable, 'scripts/bootstrap_schema.py', '--help'], text=True, capture_output=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert '--swagger-dir' in result.stdout
    assert '--output-dir' in result.stdout


def _load_script(name):
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, Path('scripts') / f'{name}.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _indexed_documents(tmp_path, monkeypatch):
    """Run the real builder with a deterministic encoder adapter; no model download."""
    import numpy as np
    from types import ModuleType
    docs = tmp_path / 'docs'
    docs.mkdir()
    (docs / 'guide.md').write_text('SYNTHETICLOOKUP uses the approval workflow.')
    database = tmp_path / 'docs.db'
    _load_script('build_docs_index').build_docs_index(docs, database)
    calls = []
    class Encoder:
        def __init__(self, model, **kwargs):
            calls.append(('init', model, kwargs))
        def encode(self, texts, **kwargs):
            calls.append(('encode', list(texts), kwargs))
            return np.asarray([[1.,0.,0.] for _ in texts], dtype=np.float32)
    adapter = ModuleType('sentence_transformers')
    adapter.SentenceTransformer = Encoder
    monkeypatch.setitem(sys.modules,'sentence_transformers',adapter)
    output = tmp_path / 'vectors'
    manifest = _load_script('build_document_vectors').build_document_vectors(database,output,'synthetic-chosen-model', local_files_only=True)
    assert manifest['model'] == 'synthetic-chosen-model'
    assert manifest['dimension'] == 3
    assert manifest['count'] == 1
    return database,output,calls,Encoder


def test_optional_vector_build_and_query_use_same_chosen_model(tmp_path,monkeypatch):
    from epicor_mcp.index.docs_index import DocsIndex
    database,output,calls,_ = _indexed_documents(tmp_path,monkeypatch)
    with DocsIndex(database,semantic_dir=output,semantic_model='synthetic-chosen-model') as index:
        result = index.search('a natural language question')
        assert result and index.last_search_mode == 'semantic'
        assert result[0]['content'].startswith('SYNTHETICLOOKUP')
        assert result[0]['score'] == 1.
    inits = [call for call in calls if call[0] == 'init']
    assert len(inits) == 2
    assert all(call[1] == 'synthetic-chosen-model' for call in inits)
    assert inits[1][2] == {'device':'cpu','local_files_only':True}
    assert all(call[2]['normalize_embeddings'] for call in calls if call[0] == 'encode')


@pytest.mark.parametrize('problem',['model','stale','missing','dimension','query_dimension'])
def test_bad_or_missing_optional_vectors_fall_back_to_real_substring(tmp_path,monkeypatch,problem):
    import json
    import sqlite3
    import numpy as np
    from epicor_mcp.index.docs_index import DocsIndex
    database,output,_calls,encoder = _indexed_documents(tmp_path,monkeypatch)
    selected_model = 'synthetic-chosen-model'
    if problem == 'model':
        selected_model = 'other-model'
    elif problem == 'stale':
        with sqlite3.connect(database) as conn:
            conn.execute("UPDATE doc_chunks SET content = content || ' changed'")
    elif problem == 'missing':
        (output / 'vectors.npy').unlink()
    elif problem == 'dimension':
        np.save(output/'vectors.npy',np.asarray([[1.,0.]],dtype=np.float32))
    elif problem == 'query_dimension':
        monkeypatch.setattr(encoder,'encode',lambda self,texts,**kw: np.asarray([[1.,0.]],dtype=np.float32))
    with DocsIndex(database,semantic_dir=output,semantic_model=selected_model) as index:
        result = index.search('theticlook')
        assert result and result[0]['content'].startswith('SYNTHETICLOOKUP')
        assert index.last_search_mode == 'substring'
        assert index.semantic_error
