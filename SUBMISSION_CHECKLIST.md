# ScoreMe hackathon submission checklist

- [ ] Replace `rollno` in the email subject with your actual MNNIT roll number. Required format: `assignment_mnnit_scoreme_<your_roll_number>` (all lowercase).
- [ ] Create a public GitHub repository, e.g. `scoreme-resilient-decision-system`.
- [ ] Push the contents of this project directory (do not commit `.venv`, `__pycache__`, `.pytest_cache`, or the local SQLite database).
- [ ] Confirm the GitHub repository contains `README.md`, `docs/ARCHITECTURE.md`, `docs/DECISION_EXAMPLES.md`, `configs/`, `app/`, and `tests/`.
- [ ] Run `pip install -r requirements.txt`, `pytest -q`, and `python run.py` once on your own machine.
- [ ] Open `/docs` and demonstrate one approved request, one rejected/manual-review request, a duplicate idempotency-key replay, an idempotency-key conflict (`409`), a transient retry, and resolution of a manual review.
- [ ] Demonstrate configuration history by registering a new rule version and reading the previous version from `/workflows/{name}/versions/{version}`.
- [ ] Email the repository URL to `it@scoreme.in` before the stated 7:00 PM deadline. Include your name and roll number in the email body.

Suggested email body:

Dear ScoreMe Team,

Please find below my GitHub submission for the Resilient Decision System hackathon assignment.

GitHub Repository: <PASTE_REPOSITORY_URL>
Name: <YOUR_NAME>
Roll Number: <YOUR_ROLL_NUMBER>
Institute: MNNIT Allahabad

Regards,
<YOUR_NAME>
