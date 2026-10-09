"""Speed, without losing correctness.

Each optimisation here replaced slow code that was obviously right with fast
code that has to be shown to give the same answer. A count that is quick but
off by one is worse than a slow one — it is a number on a button that someone
acts on. So most of these tests do not time anything; they check that the fast
path and the slow path agree.
"""

import threading

import pytest

import http_pool
import storage
import verification
from storage import LeadStore
from verification import EmailVerifier


def lead(**overrides):
    base = {"NPI Number": "1234567890", "Provider First Name": "Jane",
            "Provider Last Name": "Smith", "State": "TX",
            "Source": "Psychology Today"}
    base.update(overrides)
    return base


def seed(store):
    """A small database with one lead in each state the filters distinguish."""
    store.add_leads_batch([
        lead(**{"NPI Number": "1111111111", "Email": "a@x.example",
                "Email Status": "valid", "Phone": "(512) 471-3300"}),
        lead(**{"NPI Number": "2222222222", "Email": "b@x.example",
                "Phone": "(512) 471-3301"}),                        # unverified
        lead(**{"NPI Number": "3333333333", "Phone": "(512) 471-3302"}),  # no email
        lead(**{"NPI Number": "PT-4444444", "Phone": "(512) 471-3303"}),  # placeholder NPI
        lead(**{"NPI Number": "5555555555", "State": "OH",
                "Source": "NPI Registry", "Email": "c@y.example",
                "Email Status": "role", "NPI Checked": "2026-09-15",
                "Phone Verified": storage.PHONE_NO_RECORD}),       # both passes already ran
    ])


# ------------------------------------------------------- counting in SQL

class TestCountingInTheDatabase:
    """count() used to build every matching lead as a dictionary to count it.
    It now asks the database. These prove the two answers are identical for
    every filter the window and the passes actually use."""

    FILTERS = [
        {},
        {"missing_email": True},
        {"has_email": True},
        {"unverified": True},
        {"needs_cms": True},
        {"needs_npi": True},
        {"state": "OH"},
        {"source": "NPI Registry"},
        {"min_score": 20},
        {"email_statuses": ["valid", "role"]},
        {"sheet_key": "Texas - Psychology Today"},
        {"has_email": True, "state": "TX"},
    ]

    @pytest.mark.parametrize("filters", FILTERS,
                             ids=lambda f: ",".join(sorted(f)) or "no filter")
    def test_counting_agrees_with_listing(self, store, filters):
        seed(store)
        listed = sum(1 for _ in store.iter_leads(**filters))
        assert store.count(**dict(filters)) == listed

    def test_ordering_and_limit_do_not_change_a_count(self, store):
        seed(store)
        assert store.count(order_by="name", limit=1) == 5

    def test_pending_work_matches_what_each_pass_would_select(self, store):
        """The number on each Enrich button has to be exactly the number of
        leads that button will work through."""
        seed(store)
        pending = store.pending_counts()
        for name in LeadStore.PENDING_FILTERS:
            assert pending[name] == store.count(**{name: True}), name

    def test_the_seeded_backlogs_are_what_they_should_be(self, store):
        seed(store)
        assert store.pending_counts() == {
            "missing_email": 2,   # 3333333333 and the placeholder
            "unverified": 1,      # b@x.example
            "needs_npi": 4,       # all but the one already matched
            "needs_cms": 3,       # real NPIs not yet looked up; not the placeholder
            "needs_site_check": 0,  # none of the seeded leads has a website
        }

    def test_an_empty_database_has_nothing_pending(self, store):
        assert store.pending_counts() == {
            "missing_email": 0, "unverified": 0, "needs_npi": 0, "needs_cms": 0,
            "needs_site_check": 0}


# ---------------------------------------------------- the change fingerprint

class TestDataVersion:
    """The window skips redrawing when this is unchanged, so it must change for
    every write — and must not change for reads, or the skip never happens."""

    def test_reading_does_not_change_it(self, store):
        seed(store)
        before = store.data_version()
        list(store.iter_leads())
        store.get_stats()
        store.pending_counts()
        assert store.data_version() == before

    def test_adding_a_lead_changes_it(self, store):
        before = store.data_version()
        store.add_leads_batch([lead()])
        assert store.data_version() != before

    def test_updating_a_lead_changes_it(self, store):
        store.add_leads_batch([lead(Email="a@x.example")])
        row = next(store.iter_leads())
        before = store.data_version()
        store.update_lead(row["_id"], {"Email Status": "valid"})
        assert store.data_version() != before

    def test_a_suppression_changes_it(self, store):
        before = store.data_version()
        store.suppress("someone@practice.example")
        assert store.data_version() != before

    def test_a_write_from_a_worker_thread_changes_it(self, store):
        """Runs write from worker threads, each with its own connection. Those
        commits must show up on the window's connection too."""
        before = store.data_version()
        worker = threading.Thread(target=lambda: store.add_leads_batch([lead()]))
        worker.start()
        worker.join()
        assert store.data_version() != before


# ------------------------------------------------------- network memory

class FakeSocket:
    """Stands in for the port-25 preflight connection."""

    connects = []
    blocked = True
    last_timeout = None

    def __init__(self, *args, **kwargs):
        pass

    def settimeout(self, timeout):
        FakeSocket.last_timeout = timeout

    def connect(self, address):
        FakeSocket.connects.append(address)
        if FakeSocket.blocked:
            raise OSError("timed out")

    def close(self):
        pass


@pytest.fixture
def fake_socket(monkeypatch):
    FakeSocket.connects = []
    FakeSocket.blocked = True
    FakeSocket.last_timeout = None
    monkeypatch.setattr(verification.socket, "socket", FakeSocket)
    return FakeSocket


class TestNetworkMemory:
    """Every run used to rediscover the same facts about the network — 11 seconds
    of waiting before any work on a connection where the answer never changes."""

    def test_a_blocked_port_is_not_rediscovered_by_the_next_verifier(
            self, fake_socket, quiet_logger):
        first = EmailVerifier(logger=quiet_logger)
        assert first._smtp_reachable() is False
        assert len(fake_socket.connects) == 1

        second = EmailVerifier(logger=quiet_logger)
        assert second._smtp_reachable() is False
        assert second.smtp_blocked is True
        assert len(fake_socket.connects) == 1, "the second run waited again"

    def test_an_open_port_is_remembered_too(self, fake_socket, quiet_logger):
        fake_socket.blocked = False
        assert EmailVerifier(logger=quiet_logger)._smtp_reachable() is True
        assert EmailVerifier(logger=quiet_logger)._smtp_reachable() is True
        assert len(fake_socket.connects) == 1

    def test_the_memory_survives_closing_the_app(self, fake_socket, quiet_logger):
        EmailVerifier(logger=quiet_logger)._smtp_reachable()
        EmailVerifier._network_state = None          # as if freshly started
        assert EmailVerifier(logger=quiet_logger)._smtp_reachable() is False
        assert len(fake_socket.connects) == 1

    def test_old_memory_is_checked_again(self, fake_socket, quiet_logger, monkeypatch):
        EmailVerifier(logger=quiet_logger)._smtp_reachable()
        monkeypatch.setattr(EmailVerifier, "NETWORK_STATE_TTL", -1)
        EmailVerifier(logger=quiet_logger)._smtp_reachable()
        assert len(fake_socket.connects) == 2

    def test_health_check_makes_the_next_run_look_again(self, fake_socket, quiet_logger):
        """Connecting a VPN has to take effect when you check, not hours later."""
        EmailVerifier(logger=quiet_logger)._smtp_reachable()
        EmailVerifier.forget_network_state()
        fake_socket.blocked = False
        assert EmailVerifier(logger=quiet_logger)._smtp_reachable() is True
        assert len(fake_socket.connects) == 2

    def test_the_preflight_does_not_wait_longer_than_it_needs_to(
            self, fake_socket, quiet_logger):
        EmailVerifier(logger=quiet_logger)._smtp_reachable()
        assert fake_socket.last_timeout == EmailVerifier.PREFLIGHT_TIMEOUT
        assert EmailVerifier.PREFLIGHT_TIMEOUT <= 5

    def test_a_working_resolver_is_reused(self, monkeypatch, quiet_logger):
        dns_resolver = pytest.importorskip("dns.resolver")
        calls = []
        monkeypatch.setattr(dns_resolver.Resolver, "resolve",
                            lambda self, name, rtype="A", **kw: calls.append(name) or [])

        assert EmailVerifier(logger=quiet_logger)._pick_resolver() is not None
        assert len(calls) == 1
        assert EmailVerifier(logger=quiet_logger)._pick_resolver() is not None
        assert len(calls) == 1, "the second run probed DNS again"

    def test_a_failed_resolver_is_not_remembered(self, monkeypatch, quiet_logger):
        """A failure may just be a moment without internet. Remembering it would
        quietly switch verification off for hours afterwards."""
        dns_resolver = pytest.importorskip("dns.resolver")
        calls = []

        def refuse(self, name, rtype="A", **kw):
            calls.append(name)
            raise OSError("no answer")

        monkeypatch.setattr(dns_resolver.Resolver, "resolve", refuse)
        assert EmailVerifier(logger=quiet_logger)._pick_resolver() is None
        first_attempts = len(calls)
        assert EmailVerifier(logger=quiet_logger)._pick_resolver() is None
        assert len(calls) == first_attempts * 2


# ----------------------------------------------------- connection pooling

class TestConnectionPooling:

    def test_one_thread_reuses_its_session(self):
        assert http_pool.session() is http_pool.session()

    def test_each_worker_thread_has_its_own(self):
        """requests.Session is not safe to share across threads, and every pool
        here is concurrent."""
        sessions = []
        ready = threading.Barrier(3)

        def grab():
            ready.wait()
            sessions.append(http_pool.session())

        threads = [threading.Thread(target=grab) for _ in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert len({id(s) for s in sessions}) == 3

    def test_site_pages_and_registry_lookups_go_through_the_pool(
            self, monkeypatch, quiet_logger):
        from email_engine import EmailEngine
        from npi_engine import NPIEngine

        class Response:
            status_code = 200
            text = "<html>ok</html>"

            def json(self):
                return {"results": []}

            def raise_for_status(self):
                pass

        class Session:
            def __init__(self):
                self.calls = []

            def get(self, url, **kwargs):
                self.calls.append((url, kwargs))
                return Response()

        pooled = Session()
        monkeypatch.setattr(http_pool, "session", lambda: pooled)

        assert EmailEngine._fetch("https://practice.example/") == "<html>ok</html>"
        assert pooled.calls[-1][1]["timeout"] == (5, 10), \
            "a dead site should fail fast on connect"

        assert NPIEngine(logger=quiet_logger)._make_request(
            {"number": "1234567890"}) == {"results": []}
        assert len(pooled.calls) == 2
