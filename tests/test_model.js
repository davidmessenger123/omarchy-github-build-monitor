const assert = require("assert")
const Model = require("../Model.js")

const previous = {
  results: [{ repo: "owner/repo", runs: [{ status: "completed", conclusion: "success" }] }],
  notifications: [{ id: "1", updated_at: "2026-01-01T00:00:00Z", reason: "mention", title: "hello", repo: "owner/repo" }]
}
const unchanged = Model.deriveState(
  JSON.stringify({ repo: "owner/repo", __unchanged: true }) + "\n" +
  JSON.stringify({ __notifications: "unchanged" }) + "\n" +
  JSON.stringify({ __meta: { session: "s1", repoCount: 1 } }) + "\n",
  previous
)
assert.strictEqual(unchanged.results[0].repo, "owner/repo")
assert.deepStrictEqual(unchanged.notifications[0], {
  id: '1', reason: 'mention', title: 'hello', type: 'Notification',
  repo: 'owner/repo', html: '', body: '', updated_at: '2026-01-01T00:00:00Z'
})
assert.strictEqual(unchanged.session, "s1")

const malformed = Model.deriveState("not-json\n" + JSON.stringify({ repo: "owner/repo", runs: [null, { status: "completed", conclusion: "success" }] }))
assert.strictEqual(Model.deriveState('{}').hasMeta, false)
assert.strictEqual(Model.deriveState(JSON.stringify({ __meta: { session: 's' } })).hasMeta, true)
assert.strictEqual(malformed.results.length, 1)
assert.strictEqual(malformed.results[0].runs.length, 1)

const lines = []
for (let i = 0; i < 150; i++) lines.push(JSON.stringify({ repo: "owner/repo" + i, runs: [] }))
const capped = Model.deriveState(lines.join("\n"))
assert.strictEqual(capped.results.length, 100)

const enterprise = Model.deriveState(
  JSON.stringify({ repo: "owner/repo", runs: [], repoUrl: "https://ghe.example.test/owner/repo/actions" }) + "\n" +
  JSON.stringify({ __meta: { webOrigin: "https://ghe.example.test", repoCount: 1 } }) + "\n"
)
assert.strictEqual(enterprise.webOrigin, "https://ghe.example.test")
assert.strictEqual(enterprise.results[0].repoUrl, "https://ghe.example.test/owner/repo/actions")
const enterpriseWithoutMetaOrigin = Model.deriveState(
  JSON.stringify({ repo: "owner/repo", runs: [], repoUrl: "https://ghe.example.test/owner/repo/actions" }) + "\n" +
  JSON.stringify({ __meta: { repoCount: 1 } }) + "\n",
  {},
  "https://ghe.example.test"
)
assert.strictEqual(enterpriseWithoutMetaOrigin.webOrigin, "https://ghe.example.test")
assert.strictEqual(Model.safeWebUrl("https://evil.example/owner/repo", "https://ghe.example.test"), "")
assert.strictEqual(Model.githubAccountReposUrl("owner", "https://ghe.example.test"), "https://ghe.example.test/owner?tab=repositories")
assert.strictEqual(Model.canonicalApiHost("https://API.GITHUB.COM/"), "api.github.com")
assert.strictEqual(Model.canonicalApiHost("https://api.github.com:443/"), "api.github.com")
assert.strictEqual(Model.canonicalApiHost("https://127.0.0.1/"), "")
assert.strictEqual(Model.clampInt("6junk", 1, 10), 1)
assert.strictEqual(Model.scriptPath("file:///tmp/helper.py"), "/tmp/helper.py")
assert.strictEqual(Model.scriptPath("/tmp/helper.py"), "")
assert.strictEqual(Model.isGitHubApiHost("ghe.example.test"), false)
assert.strictEqual(Model.githubActionsUrl("owner/repo", "http://evil.example"), "")
assert.strictEqual(Model.safeWebUrl("https://ghe.example.test/a#fragment", "https://ghe.example.test"), "")

console.log("Model.js tests passed")
