import datetime as dt
import os
import threading
import unittest
from unittest.mock import patch

from flask import Flask

from agent_api.auth import AuthError, issue_login, redeem, token_hash


class FakeBubble:
    def __init__(self): self.rows=[]; self.users={"u1":{"_id":"u1"}}; self.lock=threading.Lock()
    def create(self, kind, payload):
        with self.lock:
            row={"_id":f"s{len(self.rows)+1}",**payload}; self.rows.append(row); return row["_id"]
    def search(self, kind, constraints, limit=100, **_kwargs):
        found=[r.copy() for r in self.rows if all(r.get(c["key"])==c["value"] for c in constraints)]
        return found[:limit],max(0,len(found)-limit)
    def patch(self, kind, record_id, payload):
        with self.lock:
            next(r for r in self.rows if r["_id"]==record_id).update(payload)
    def one(self, kind, record_id):
        if kind=="user": return self.users.get(record_id)
        row=next((r for r in self.rows if r["_id"]==record_id),None); return row.copy() if row else None


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.app=Flask(__name__); self.client=FakeBubble()
        self.env=patch.dict(os.environ,{"AGENT_APP_ENABLED":"true","AGENT_APP_ALLOWED_USERS":"u1","AGENT_APP_ALLOWED_ORIGINS":"https://app.test","AGENT_REDEMPTION_SINGLE_PROCESS":"true","WEB_CONCURRENCY":"1","AGENT_LOGIN_RATE_LIMIT_SECONDS":"0"},clear=False); self.env.start()
    def tearDown(self): self.env.stop()

    def issue(self):
        with self.app.test_request_context(): return issue_login("u1",self.client)

    def redeem(self, token):
        with self.app.test_request_context(headers={"Origin":"https://app.test"}): return redeem(token,self.client)

    def test_login_token_is_only_stored_as_hash_and_session_is_distinct(self):
        token=self.issue(); self.assertNotIn(token,str(self.client.rows)); self.assertEqual(self.client.rows[0]["token_hash"],token_hash(token))
        session,_=self.redeem(token); self.assertNotEqual(token,session); self.assertNotIn(session,str(self.client.rows))

    def test_replay_is_rejected(self):
        token=self.issue(); self.redeem(token)
        with self.assertRaises(AuthError): self.redeem(token)

    def test_expired_and_revoked_tokens_are_rejected(self):
        for mutation in ({"expires_at":(dt.datetime.now(dt.timezone.utc)-dt.timedelta(minutes=1)).isoformat()},{"revoked":True}):
            token=self.issue(); self.client.rows[-1].update(mutation)
            with self.assertRaises(AuthError): self.redeem(token)

    def test_empty_allowlist_denies(self):
        with patch.dict(os.environ,{"AGENT_APP_ALLOWED_USERS":""}):
            with self.app.test_request_context(),self.assertRaises(AuthError): issue_login("u1",self.client)

    def test_wrong_origin_denies_redemption(self):
        token=self.issue()
        with self.app.test_request_context(headers={"Origin":"https://evil.test"}),self.assertRaises(AuthError): redeem(token,self.client)

    def test_concurrent_redemption_has_one_winner_in_single_process(self):
        token=self.issue(); outcomes=[]
        def work():
            try: self.redeem(token); outcomes.append("ok")
            except AuthError: outcomes.append("denied")
        threads=[threading.Thread(target=work) for _ in range(2)]
        [t.start() for t in threads]; [t.join() for t in threads]
        self.assertEqual(outcomes.count("ok"),1)


if __name__=="__main__": unittest.main()
