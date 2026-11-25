# MGU Pareeksha Result Analyzer

A mobile-friendly web application to analyze batch-wise semester results from the MG University Pareeksha portal (`pareeksha.mgu.ac.in`), count subject-wise failures, and check upcoming same-semester examinations to see if failed students passed their backlog papers later.

## Features
- **Course & Examination Selector:** Dynamically loads all courses and semester exams from MGU Pareeksha.
- **PRN Range Scanner:** Enter a 12-digit Start PRN and End PRN for your batch.
- **Subject-wise Failure Statistics:** Visual bar chart and CSV export showing failure counts per subject.
- **Upcoming Exam Backlog Tracker:** Automatically checks later examinations for the same semester to show which failed papers were passed later vs. still pending.
- **Mobile-Friendly UI:** Responsive layout for phones, tablets, and desktops.

## Run Locally
```bash
pip install -r requirements.txt
python app.py
```
Then open `http://localhost:5000`.

## Deploy as a Live Web Page (from GitHub)
Because this app queries `pareeksha.mgu.ac.in` from a Python backend (GitHub Pages only supports static HTML/JS), deploy the GitHub repository to **Render** (free tier):
1. Push this repository to GitHub.
2. Go to [render.com](https://render.com) -> **New +** -> **Web Service** -> connect your GitHub repo.
3. Render automatically reads `render.yaml` (`pip install -r requirements.txt` and `gunicorn app:app`).
4. Once deployed, open your live `.onrender.com` URL on any phone or computer.
