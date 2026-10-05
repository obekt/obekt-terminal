# Disclaimer

**Read this before you run, deploy, fund, or copy anything in this repository.**

## This is not financial advice

Nothing in this repository is financial, investment, legal, or tax advice. It is
not a recommendation or solicitation to buy or sell any asset. It is not a signal
service, a managed account, a performance guarantee, or a product for sale. It is
an **educational experiment and a portfolio piece**, published as-is.

## It is a pure experiment

This is a research prototype built to explore a specific question: *can a cheap,
typed decision-model plus deterministic risk code operate a real trading loop
autonomously, and what does that actually look like?* The answer, so far, is
"instructive" — including the parts that lose money.

The live results shown in the screenshots and sample data are **real and mixed**.
Over the sample window the system had a win rate around 38%, a profit factor
**below 1.0**, and a small **net loss** after fees. We publish the losing trades,
the red days, and the worst trade because hiding them would make this dishonest.
A scalping strategy on retail crypto spot pays a heavy fee toll, and beating it
consistently is genuinely hard. **Do not assume this system makes money. Over the
period shown, it did not.**

## Trading real money can lose all of it

Automated trading carries extreme risk:

- You can lose your **entire balance**, potentially in seconds.
- Bugs, latency, partial fills, thin order books, exchange outages, API changes,
  network partitions, and bad data can all cause losses no model can prevent.
- Leverage, if you add it, can lose **more** than you deposited.
- Past behavior — including the live history in this repo — says nothing about
  future results.
- Crypto markets are volatile, manipulated, and adversarial. Spreads widen and
  books thin exactly when you least want them to.

## You are solely responsible

If you point this code at a funded account, **every order, every fill, every
loss, and every consequence is yours alone.** The authors and contributors:

- did not test this for your exchange, your jurisdiction, your balance, or your risk tolerance;
- provide **no warranty** of any kind, express or implied (see [LICENSE](LICENSE));
- accept **no liability** for any loss, damage, or claim arising from your use of this software;
- are not your fiduciary, advisor, broker, or counterparty.

## Do your own work

- Use **demo/paper accounts** first if your venue offers them.
- Never trade money you cannot afford to lose completely.
- Understand every line before you fund it — especially the risk constants.
- Check your local laws; automated trading and crypto may be restricted or
  regulated where you live. Compliance is your responsibility.
- Keep your API keys scoped down (read + trade only, **no withdrawal**),
  IP-whitelisted, and secret. This repo never asks for withdrawal permission.

## No affiliation or endorsement

"Jev"/TypeSafe, OKX, and any model or vendor named here are the property of their
respective owners. Their mention describes what this experiment used; it is not an
endorsement of this project by them.

---

**By using this repository you acknowledge that it is an experiment, not advice,
that it can and has lost money, and that you use it entirely at your own risk.**

If any part of this is unclear, don't run it with real funds.
