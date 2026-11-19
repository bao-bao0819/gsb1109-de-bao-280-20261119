"""Build the demo database used by probe.py.

Two views over the same 1,000 row table:

case_a  every row must do enough work that the *first* page cannot finish
        inside the limit, so no CSV byte is ever produced.
case_b  rows 1-500 are trivial and rows 501-600 are expensive: pages 1-5
        stream fine, page 6 times out after 500 rows have been written.

The cost is a recursive CTE rather than sleep() so the demo needs no plugin
and the two cases interrupt on CPU progress, not on wall clock sleeps.
"""

import pathlib
import sqlite3

HERE = pathlib.Path(__file__).parent
DB = HERE / "demo.db"

EXPENSIVE = """
(with recursive c(x) as (
   select 1 union all
   select x + 1 from c where x < {n} + 0 * b.id
 ) select count(*) from c)
"""

SCHEMA = f"""
drop view if exists case_a;
drop view if exists case_b;
drop table if exists big;

create table big (id integer primary key, payload text not null);
with recursive c(x) as (select 1 union all select x + 1 from c where x < 1000)
insert into big (id, payload) select x, 'row-' || printf('%04d', x) from c;

-- Every row is expensive, so even LIMIT 101 cannot return in time.
create view case_a as
select b.id, b.payload,
       {EXPENSIVE.format(n=80000)} as pause
from big b
order by b.id;

-- Only rows 501-600 are expensive: pages 1-5 are sent before page 6 fails.
create view case_b as
select b.id, b.payload,
       case when b.id between 501 and 600
            then {EXPENSIVE.format(n=80000)}
            else 0
       end as pause
from big b
order by b.id;
"""


def main():
    conn = sqlite3.connect(DB)
    conn.executescript(SCHEMA)
    conn.close()
    print(f"wrote {DB}")


if __name__ == "__main__":
    main()
