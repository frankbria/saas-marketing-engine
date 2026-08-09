# S4.5.1 — Published content never reaches the served web root (issue #78)

**Branch:** `fix/issue-78-serve-published-content` · **Plan source:** self-authored from the
issue's acceptance criteria + codebase exploration (the issue body has AC but no step plan).

## The defect

`BlogAdapter.publish()` writes `workspace/<slug>/site/blog/<post-slug>.html`
(`app/channels/blog.py:56-71`); `PodcastAdapter.publish()` writes the MP3, sidecar, episode page
and rebuilds `feed.xml` under `workspace/<slug>/site/podcast/` (`app/channels/podcast.py:59`).
`deploy_site()` is the only thing that copies `workspace/<slug>/site` →
`nginx_sites_root/<domain>` (`app/modules/setup/site.py:89-115`), and it is called from exactly
one place: the `setup_site` job handler (`site.py:181`).

Nothing re-syncs after a publish, so every autonomously published artifact lands in a directory
nginx does not serve and every recorded `external_url` 404s.

## Architectural fork (Phase 4 — needs a human call)

**Option A — incremental sync.** Keep the copy model. Add a sync seam called after a successful
filesystem-channel publish that copies just the new artifact(s) into `nginx_sites_root/<domain>`.
Retract removes its copy too.

**Option B — serve the workspace directly (recommended).** `deploy_site()` stops copying; it emits
a vhost whose `root` is `workspace/<slug>/site` and writes only the `.conf`. One source of truth,
no sync code, retract works by construction, no `rmtree` window on a live site.

Trade-off: B forecloses serving from a different host than the engine without adding a real
rsync step later; A keeps that seam. v1 is pinned to one VPS (NFR-2/NFR-3), and nothing is
deployed yet (#80 — no deploy automation exists), so there is nothing to migrate.

## Steps (TDD — test first for each) — assuming Option B

1. **Deploy contract test** (`tests/test_site_template.py`): `deploy_site` emits a vhost whose
   `root` is the product's workspace site dir; makes no copy under `nginx_sites_root`; still
   rejects a non-hostname `marketing_domain`; still idempotent across two runs.
2. **`deploy_site`** (`app/modules/setup/site.py`): drop `rmtree`/`copytree`; point the vhost
   `root` at the workspace site dir; keep the `_HOSTNAME_RE` guard (it still guards the nginx
   `server_name` and the `.conf` filename); return the served dir. Update the module docstring
   and the `ponytail:` note.
3. **Blog reachability test** (`tests/test_publish.py` or `test_site_template.py`): publish a blog
   item → the file exists under the path the vhost `root` resolves to and the returned
   `external_url` path segment matches; retract → the file is gone from that same path.
4. **Podcast reachability test** (`tests/test_podcast_adapter.py`): publish an episode → MP3 +
   `feed.xml` exist under the vhost root; the feed enclosure URL path matches the file on disk.
5. **Vault-exposure guard** (`tests/test_site_template.py`): assert the credentials vault
   (`workspace/<slug>/vault/`) is NOT inside the served root — it is a sibling of `site/`, and a
   test pins that so a future layout change cannot silently expose it.
6. **Docs**: note the deploy model in `TECH_SPEC.md` §6.1/§11 and `infra/deploy/PORTS.md` (the
   vhost example there shows a `root` under the web root and would now be wrong).

## Acceptance criteria (from #78)

- [ ] A successful publish on a filesystem-backed channel makes the artifact reachable at the
      `external_url` the adapter returned
- [ ] Sync is incremental — publishing one post does not rewrite or `rmtree` the whole deployed
      site *(satisfied by construction under B: there is no sync step)*
- [ ] Retract removes the artifact from the served root too *(by construction under B)*
- [ ] Sync failure is surfaced *(by construction under B: the write is the publish)*
- [ ] Test: publish → file under the served root; retract → gone
- [ ] Test: podcast publish → `feed.xml` at the served root contains the new episode enclosure

## Known limitations (for the PR)

- Under B, serving from a host other than the engine's would need a real rsync step added at the
  publish seam. Out of scope; noted against #80.
- `nginx -s reload` after a vhost write remains operational/manual (#80).
