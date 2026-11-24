"""Plan store: the durable PLAN.md revision and task definitions.

Choosing which files a task touches is the planner's job, and the worker
prompt carries the goal, acceptance, the paths the planner named and the
project notes.  ``plan_store`` stays because PLAN.md scheduling still uses its
immutable, hash-verified revisions.
"""
