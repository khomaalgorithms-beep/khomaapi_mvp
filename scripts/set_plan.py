"""Set / clear a user's TEST plan override (Part 2a, before Whop is wired).

Only takes effect when ALLOW_MANUAL_PLAN=1 AND the user has NO linked Whop
membership — it can never override a real Whop subscription.

Usage:
    DATABASE_URL=postgres://...  python -m scripts.set_plan user@email.com pro
    DATABASE_URL=postgres://...  python -m scripts.set_plan user@email.com clear
    python -m scripts.set_plan --list

Valid plans: solo | pro | elite | founder | clear
"""

import sys

from app import db as dbmod
from app import entitlements as ent


def main(argv):
    con = dbmod.connect("")
    if argv and argv[0] == "--list":
        rows = con.execute(
            "SELECT email, manual_plan, subscription_status, whop_membership_id FROM users "
            "ORDER BY id"
        ).fetchall()
        for r in rows:
            print(f"{r['email']:<35} manual={r['manual_plan'] or '-':<8} "
                  f"whop_status={r['subscription_status'] or '-':<10} "
                  f"linked={'yes' if r['whop_membership_id'] else 'no'}")
        con.close()
        return

    if len(argv) != 2:
        print(__doc__)
        sys.exit(1)

    email, plan = argv[0].lower().strip(), argv[1].lower().strip()
    value = None if plan in ("clear", "none", "") else plan
    if value is not None and ent.normalize_tier(value) is None:
        print(f"Invalid plan '{plan}'. Use: solo | pro | elite | founder | clear")
        sys.exit(1)

    row = con.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
    if not row:
        print(f"No user with email {email}")
        con.close()
        sys.exit(1)

    con.execute("UPDATE users SET manual_plan=? WHERE email=?", (value, email))
    con.commit()
    con.close()
    print(f"Set manual_plan={value!r} for {email}. "
          f"(Active only if ALLOW_MANUAL_PLAN=1 and no Whop membership is linked.)")


if __name__ == "__main__":
    main(sys.argv[1:])
