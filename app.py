"""
MGU Pareeksha Result Analyzer
Scrapes exam results from pareeksha.mgu.ac.in for a range of register numbers,
analyzes subject-wise failures, and displays summary statistics.
"""

import os
import requests
from bs4 import BeautifulSoup
from flask import Flask, render_template, request, jsonify
import urllib3
import time
import re
import json
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)

BASE_URL = "https://pareeksha.mgu.ac.in/Pareeksha/index.php/Public/PareekshaResultView_ctrl/index"


def get_courses_and_exams(project_id, cs_id):
    """Fetch available courses and exams from the Pareeksha portal."""
    url = f"{BASE_URL}/{project_id}/{cs_id}"
    session = requests.Session()
    resp = session.get(url, timeout=20, verify=False)
    soup = BeautifulSoup(resp.text, 'lxml')

    courses = []
    cs_select = soup.find('select', {'name': 'cs_id'})
    if cs_select:
        for opt in cs_select.find_all('option'):
            if opt.get('value'):
                courses.append({
                    'value': opt['value'],
                    'text': opt.text.strip(),
                    'selected': opt.has_attr('selected')
                })

    exams = []
    exam_select = soup.find('select', {'name': 'exam_id'})
    if exam_select:
        for opt in exam_select.find_all('option'):
            if opt.get('value'):
                exams.append({
                    'value': opt['value'],
                    'text': opt.text.strip()
                })

    return courses, exams


def fetch_result(session, url, cs_id, exam_id, prn):
    """Fetch result for a single PRN."""
    data = {
        'cs_id': cs_id,
        'exam_id': exam_id,
        'prn': prn,
        'btnresult': 'Get Result'
    }

    try:
        resp = session.post(url, data=data, timeout=20, verify=False)
        soup = BeautifulSoup(resp.text, 'lxml')
        text = soup.get_text()

        if 'Result Not Available' in text:
            return {'prn': prn, 'status': 'not_found', 'subjects': [], 'name': ''}

        return parse_result_page(soup, prn)

    except Exception as e:
        return {'prn': prn, 'status': 'error', 'error': str(e), 'subjects': [], 'name': ''}


NON_SUBJECT_KEYWORDS = (
    'warning', 'semester result', 'scpa', 'sgpa', 'cgpa', 'total',
    'grand total', 'note:', 'javascript', 'firefox', 'internet explorer',
    'permanent register', 'name of student', 'exam centre', 'programme:'
)


def _get_direct_rows(table):
    """Return direct <tr> elements of a table (including direct <tbody> children)."""
    rows = table.find_all('tr', recursive=False)
    for tbody in table.find_all('tbody', recursive=False):
        rows.extend(tbody.find_all('tr', recursive=False))
    return rows


def _is_non_subject_row(cell_texts):
    """Return True if a row is a header, summary, warning, or note rather than a subject."""
    joined = ' '.join(cell_texts).lower().strip()
    if not joined:
        return True
    if any(kw in joined for kw in NON_SUBJECT_KEYWORDS):
        return True
    # Sub-header rows like ['ESA', 'MAX', 'ISA', 'MAX']
    upper_tokens = {c.upper().strip() for c in cell_texts if c.strip()}
    if upper_tokens and upper_tokens.issubset({'ESA', 'ISA', 'MAX', 'MIN', 'GP', 'CP', 'GRADE', 'RESULT', 'TOTAL', 'EXTERNAL', 'INTERNAL', 'COURSE', 'CODE', 'COURSE CODE'}):
        return True
    return False


def parse_result_page(soup, prn):
    """
    Parse the result page HTML to extract student name, subject-wise marks, and pass/fail status.
    """
    result = {
        'prn': prn,
        'status': 'found',
        'name': '',
        'subjects': [],
        'overall_result': '',
    }

    # Remove <noscript> blocks so browser warning tables are never parsed
    for ns in soup.find_all('noscript'):
        ns.decompose()

    full_text = soup.get_text()

    # Only inspect leaf tables (tables that do not wrap other tables)
    leaf_tables = [t for t in soup.find_all('table') if not t.find('table')]

    # Extract student name from student info table first
    for table in leaf_tables:
        for row in _get_direct_rows(table):
            cells = [c.get_text(' ', strip=True) for c in row.find_all(['th', 'td'], recursive=False)]
            if len(cells) >= 2 and 'name of student' in cells[0].lower():
                candidate = cells[-1].strip()
                if candidate and candidate != ':':
                    result['name'] = candidate
                    break
        if result['name']:
            break

    # Fallback regex for student name
    if not result['name']:
        name_patterns = [
            r'Name\s*of\s*Student\s*[:\s]+([A-Za-z][A-Za-z\s\.]+)',
            r'Student\s*Name\s*[:\-]\s*([A-Za-z][A-Za-z\s\.]+)',
            r'Candidate\s*Name\s*[:\-]\s*([A-Za-z][A-Za-z\s\.]+)',
        ]
        for pattern in name_patterns:
            match = re.search(pattern, full_text, re.IGNORECASE)
            if match:
                result['name'] = match.group(1).strip().split('\n')[0].strip()
                break

    for table in leaf_tables:
        rows = _get_direct_rows(table)
        if len(rows) < 2:
            continue

        # Check if this table has result-like headers
        header_row = rows[0]
        header_cells = header_row.find_all(['th', 'td'], recursive=False)
        raw_header_texts = [c.get_text(' ', strip=True).lower() for c in header_cells]
        header_combined = ' '.join(raw_header_texts)

        is_result_table = any(keyword in header_combined for keyword in [
            'course code', 'subject', 'paper', 'external', 'internal', 'grade', 'result'
        ]) and ('course' in header_combined or 'subject' in header_combined or 'paper' in header_combined)

        if not is_result_table:
            continue

        # Expand colspan in header cells so column indices align with data rows
        expanded_headers = []
        for cell in header_cells:
            txt = cell.get_text(' ', strip=True).lower()
            try:
                span = int(cell.get('colspan', 1))
            except (TypeError, ValueError):
                span = 1
            for _ in range(max(1, span)):
                expanded_headers.append(txt)

        col_map = {}
        for idx, text in enumerate(expanded_headers):
            if 'sl' in text or '#' in text:
                col_map.setdefault('sl', idx)
            elif 'code' in text:
                col_map.setdefault('code', idx)
            elif 'subject' in text or 'paper' in text or 'course' in text:
                col_map.setdefault('subject', idx)
            elif 'internal' in text or 'isa' in text or text in ('ia', 'ce'):
                col_map.setdefault('internal', idx)
            elif 'external' in text or 'ese' in text or text in ('ee', 'end'):
                col_map.setdefault('external', idx)
            elif 'total' in text:
                col_map.setdefault('total', idx)
            elif 'grade' in text and 'point' not in text:
                col_map.setdefault('grade', idx)
            elif 'result' in text or 'status' in text:
                col_map.setdefault('result', idx)
            elif 'credit' in text:
                col_map.setdefault('credit', idx)

        # Parse data rows
        for row in rows[1:]:
            cells = row.find_all(['th', 'td'], recursive=False)
            if len(cells) < 4:
                continue

            cell_texts = [c.get_text(' ', strip=True) for c in cells]
            if _is_non_subject_row(cell_texts):
                continue

            subject_info = {}
            if 'code' in col_map and col_map['code'] < len(cell_texts):
                subject_info['code'] = cell_texts[col_map['code']]

            if 'subject' in col_map and col_map['subject'] < len(cell_texts):
                subject_info['name'] = cell_texts[col_map['subject']]
            elif len(cell_texts) >= 2:
                subject_info['name'] = cell_texts[1]

            if not subject_info.get('name'):
                continue

            if 'internal' in col_map and col_map['internal'] < len(cell_texts):
                subject_info['internal'] = cell_texts[col_map['internal']]
            if 'external' in col_map and col_map['external'] < len(cell_texts):
                subject_info['external'] = cell_texts[col_map['external']]
            if 'total' in col_map and col_map['total'] < len(cell_texts):
                subject_info['total'] = cell_texts[col_map['total']]
            if 'grade' in col_map and col_map['grade'] < len(cell_texts):
                subject_info['grade'] = cell_texts[col_map['grade']]
            if 'result' in col_map and col_map['result'] < len(cell_texts):
                subject_info['result_status'] = cell_texts[col_map['result']]
            elif cell_texts:
                subject_info['result_status'] = cell_texts[-1]
            if 'credit' in col_map and col_map['credit'] < len(cell_texts):
                subject_info['credit'] = cell_texts[col_map['credit']]

            subject_info['failed'] = is_subject_failed(subject_info, cell_texts)
            result['subjects'].append(subject_info)

    # Determine overall result
    overall_patterns = [
        r'SEMESTER\s*RESULT.*?((?:Pass|Fail|Passed|Failed))',
        r'Overall\s*Result\s*[:\-]\s*(Pass|Fail|Passed|Failed)',
    ]
    for pattern in overall_patterns:
        match = re.search(pattern, full_text, re.IGNORECASE | re.DOTALL)
        if match:
            result['overall_result'] = match.group(1).strip()
            break

    if not result['subjects']:
        result = parse_result_alternative(soup, prn, result)

    return result


def parse_result_alternative(soup, prn, result):
    """
    Alternative parser for result pages with non-standard layouts.
    """
    bord_tables = soup.find_all('table', class_='bord_rslt')
    for table in bord_tables:
        if table.find('table'):
            continue
        for row in _get_direct_rows(table):
            cells = row.find_all(['th', 'td'], recursive=False)
            cell_texts = [c.get_text(' ', strip=True) for c in cells]
            if len(cell_texts) < 4 or _is_non_subject_row(cell_texts):
                continue
            subject_info = {
                'name': cell_texts[1] if len(cell_texts) > 1 else '',
                'failed': False
            }
            for ct in cell_texts:
                ct_upper = ct.upper()
                if ct_upper in ['F', 'FAIL', 'FAILED', 'AB', 'ABSENT', 'FE']:
                    subject_info['failed'] = True
                if ct_upper in ['P', 'PASS', 'PASSED']:
                    subject_info['failed'] = False

            if subject_info.get('name'):
                result['subjects'].append(subject_info)

    return result


def is_subject_failed(subject_info, cell_texts):
    """Determine if a subject is failed based on available data."""
    # Check explicit result status
    result_status = subject_info.get('result_status', '').upper()
    if result_status in ['F', 'FAIL', 'FAILED', 'FE']:
        return True
    if result_status in ['P', 'PASS', 'PASSED']:
        return False

    # Check grade
    grade = subject_info.get('grade', '').upper()
    if grade in ['F', 'FE', 'AB', 'ABSENT']:
        return True
    if grade in ['A+', 'A', 'B+', 'B', 'C+', 'C', 'D', 'E', 'S', 'O', 'P']:
        return False

    # Check if any cell text indicates failure
    for text in cell_texts:
        t = text.upper().strip()
        if t in ['F', 'FAIL', 'FAILED', 'FE', 'AB', 'ABSENT']:
            return True

    # Check if marks are below passing (usually 40%)
    total = subject_info.get('total', '')
    if total and total.isdigit():
        # This is a rough heuristic; actual passing marks depend on the scheme
        pass

    return False


def extract_semester_number(exam_text):
    """Extract canonical semester number (1-10) from an examination name."""
    if not exam_text:
        return None
    text = exam_text.lower()

    word_map = {
        'first': 1, '1st': 1, 'i ': 1, 'sem 1': 1, 'semester 1': 1, 'semester i ': 1,
        'second': 2, '2nd': 2, 'ii ': 2, 'sem 2': 2, 'semester 2': 2, 'semester ii ': 2,
        'third': 3, '3rd': 3, 'iii ': 3, 'sem 3': 3, 'semester 3': 3, 'semester iii ': 3,
        'fourth': 4, '4th': 4, 'iv ': 4, 'sem 4': 4, 'semester 4': 4, 'semester iv ': 4,
        'fifth': 5, '5th': 5, 'v ': 5, 'sem 5': 5, 'semester 5': 5, 'semester v ': 5,
        'sixth': 6, '6th': 6, 'vi ': 6, 'sem 6': 6, 'semester 6': 6, 'semester vi ': 6,
        'seventh': 7, '7th': 7, 'vii ': 7, 'sem 7': 7, 'semester 7': 7, 'semester vii ': 7,
        'eighth': 8, '8th': 8, 'viii ': 8, 'sem 8': 8, 'semester 8': 8, 'semester viii ': 8,
        'ninth': 9, '9th': 9, 'ix ': 9, 'sem 9': 9, 'semester 9': 9, 'semester ix ': 9,
        'tenth': 10, '10th': 10, 'x ': 10, 'sem 10': 10, 'semester 10': 10, 'semester x ': 10,
    }

    # Check word-based semester indicators first (longest match priority)
    for key in ['tenth', 'ninth', 'eighth', 'seventh', 'sixth', 'fifth', 'fourth', 'third', 'second', 'first',
                '10th', '9th', '8th', '7th', '6th', '5th', '4th', '3rd', '2nd', '1st',
                'semester 10', 'semester 9', 'semester 8', 'semester 7', 'semester 6',
                'semester 5', 'semester 4', 'semester 3', 'semester 2', 'semester 1',
                'sem 10', 'sem 9', 'sem 8', 'sem 7', 'sem 6', 'sem 5', 'sem 4', 'sem 3', 'sem 2', 'sem 1']:
        if re.search(r'\b' + re.escape(key) + r'\b', text):
            return word_map[key]

    # Check Roman numerals next to "semester"
    roman_match = re.search(r'\bsemester\s+(x|ix|viii|vii|vi|v|iv|iii|ii|i)\b', text)
    if roman_match:
        roman_map = {'i': 1, 'ii': 2, 'iii': 3, 'iv': 4, 'v': 5, 'vi': 6, 'vii': 7, 'viii': 8, 'ix': 9, 'x': 10}
        return roman_map.get(roman_match.group(1))

    return None


def get_upcoming_exams(exams, selected_exam_id):
    """Return exams that appear after selected_exam_id for the same semester."""
    selected_idx = None
    selected_text = ''
    for idx, ex in enumerate(exams):
        if str(ex['value']) == str(selected_exam_id):
            selected_idx = idx
            selected_text = ex['text']
            break

    if selected_idx is None:
        return []

    target_sem = extract_semester_number(selected_text)
    subsequent = exams[selected_idx + 1:]

    if target_sem is None:
        return subsequent

    return [ex for ex in subsequent if extract_semester_number(ex['text']) == target_sem]


def normalize_subject_name(name):
    """Normalize a subject name/code for cross-exam comparison."""
    if not name:
        return ''
    cleaned = re.sub(r'[^a-z0-9\s]', ' ', name.lower())
    return re.sub(r'\s+', ' ', cleaned).strip()


def subjects_match(name_a, name_b):
    """Check whether two subject names refer to the same paper."""
    na = normalize_subject_name(name_a)
    nb = normalize_subject_name(name_b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    if len(na) >= 5 and len(nb) >= 5 and (na in nb or nb in na):
        return True
    return False


def check_upcoming_for_failed_students(session, url, cs_id, failed_students, upcoming_exams, delay=0.5):
    """
    For each student with failed subjects, check upcoming exams in chronological order
    to see if they later passed each failed paper.
    """
    if not failed_students or not upcoming_exams:
        return {}

    student_tracking = {}
    for student in failed_students:
        prn = student['prn']
        papers = {}
        for subj_name in student.get('failed_subjects', []):
            papers[subj_name] = {
                'subject': subj_name,
                'cleared': False,
                'cleared_in': None,
                'cleared_exam_id': None,
                'new_external': '',
                'new_internal': '',
                'new_grade': '',
                'new_total': '',
                'attempts': []
            }
        student_tracking[prn] = papers

    for student in failed_students:
        prn = student['prn']
        papers = student_tracking[prn]

        for exam in upcoming_exams:
            # Stop checking more exams once all failed papers for this student are cleared
            if all(p['cleared'] for p in papers.values()):
                break

            res = fetch_result(session, url, cs_id, exam['value'], prn)
            if delay > 0:
                time.sleep(delay)

            if res.get('status') != 'found':
                continue

            for subj in res.get('subjects', []):
                for orig_name, paper_info in papers.items():
                    if paper_info['cleared']:
                        continue
                    if subjects_match(orig_name, subj.get('name', '')):
                        paper_info['attempts'].append({
                            'exam_id': exam['value'],
                            'exam_name': exam['text'],
                            'failed': subj.get('failed', False),
                            'external': subj.get('external', ''),
                            'internal': subj.get('internal', ''),
                            'grade': subj.get('grade', ''),
                            'total': subj.get('total', '')
                        })
                        if not subj.get('failed', False):
                            paper_info['cleared'] = True
                            paper_info['cleared_in'] = exam['text']
                            paper_info['cleared_exam_id'] = exam['value']
                            paper_info['new_external'] = subj.get('external', '')
                            paper_info['new_internal'] = subj.get('internal', '')
                            paper_info['new_grade'] = subj.get('grade', '')
                            paper_info['new_total'] = subj.get('total', '')

    return student_tracking


def apply_upcoming_tracking(analysis, student_tracking, upcoming_exams):
    """Merge upcoming exam tracking results into the analysis summary."""
    analysis['upcoming_checked'] = True
    analysis['upcoming_exams_checked'] = [e['text'] for e in upcoming_exams]

    students_cleared_all = 0
    students_still_failed = 0

    # Track per-subject cleared counts
    subject_cleared_counts = defaultdict(int)

    for student in analysis.get('failed_students', []):
        prn = student['prn']
        papers = student_tracking.get(prn, {})
        paper_statuses = []
        cleared_count = 0
        still_failed_subjects = []

        for subj_name in student.get('failed_subjects', []):
            info = papers.get(subj_name, {
                'subject': subj_name,
                'cleared': False,
                'cleared_in': None,
                'cleared_exam_id': None,
                'new_external': '',
                'new_internal': '',
                'new_grade': '',
                'new_total': '',
                'attempts': []
            })
            paper_statuses.append(info)
            if info['cleared']:
                cleared_count += 1
                subject_cleared_counts[subj_name] += 1
            else:
                still_failed_subjects.append(subj_name)

        student['paper_statuses'] = paper_statuses
        student['cleared_count'] = cleared_count
        student['still_failed_subjects'] = still_failed_subjects
        student['all_cleared'] = len(still_failed_subjects) == 0

        if student['all_cleared']:
            students_cleared_all += 1
        else:
            students_still_failed += 1

    # Also sync paper_statuses into all_students
    failed_by_prn = {s['prn']: s for s in analysis.get('failed_students', [])}
    for student in analysis.get('all_students', []):
        if student['prn'] in failed_by_prn:
            fs = failed_by_prn[student['prn']]
            student['paper_statuses'] = fs['paper_statuses']
            student['still_failed_subjects'] = fs['still_failed_subjects']
            student['all_cleared'] = fs['all_cleared']

    # Update subject_failures with cleared_later & still_failed counts
    for subj_name, subj_data in analysis.get('subject_failures', {}).items():
        cleared = subject_cleared_counts.get(subj_name, 0)
        subj_data['cleared_later'] = cleared
        subj_data['still_failed'] = max(0, subj_data['count'] - cleared)

    analysis['subject_failures_sorted'] = sorted(
        analysis['subject_failures'].items(),
        key=lambda x: x[1]['count'],
        reverse=True
    )

    analysis['students_cleared_all'] = students_cleared_all
    analysis['students_still_failed'] = students_still_failed
    return analysis


def analyze_results(results):
    """Analyze a list of results to generate failure statistics."""
    analysis = {
        'total_students_checked': len(results),
        'results_found': 0,
        'results_not_found': 0,
        'results_error': 0,
        'students_with_failures': 0,
        'all_pass_students': 0,
        'subject_failures': defaultdict(lambda: {'count': 0, 'students': []}),
        'failed_students': [],
        'all_students': [],
    }

    for r in results:
        if r['status'] == 'not_found':
            analysis['results_not_found'] += 1
            continue
        elif r['status'] == 'error':
            analysis['results_error'] += 1
            continue

        analysis['results_found'] += 1

        student_info = {
            'prn': r['prn'],
            'name': r.get('name', ''),
            'failed_subjects': [],
            'total_subjects': len(r.get('subjects', [])),
            'subjects': r.get('subjects', [])
        }

        has_failure = False
        for subj in r.get('subjects', []):
            if subj.get('failed', False):
                has_failure = True
                subj_name = subj.get('name', 'Unknown')
                student_info['failed_subjects'].append(subj_name)
                analysis['subject_failures'][subj_name]['count'] += 1
                analysis['subject_failures'][subj_name]['students'].append({
                    'prn': r['prn'],
                    'name': r.get('name', '')
                })

        if has_failure:
            analysis['students_with_failures'] += 1
            analysis['failed_students'].append(student_info)
        else:
            analysis['all_pass_students'] += 1

        analysis['all_students'].append(student_info)

    # Convert defaultdict to regular dict for JSON serialization
    analysis['subject_failures'] = dict(analysis['subject_failures'])

    # Sort subject failures by count (descending)
    analysis['subject_failures_sorted'] = sorted(
        analysis['subject_failures'].items(),
        key=lambda x: x[1]['count'],
        reverse=True
    )

    return analysis


@app.route('/')
def index():
    """Main page."""
    return render_template('index.html')


@app.route('/api/load_options', methods=['POST'])
def load_options():
    """Load courses and exams for a given project/course."""
    project_id = request.json.get('project_id', '3')
    cs_id = request.json.get('cs_id', '421')

    try:
        courses, exams = get_courses_and_exams(project_id, cs_id)
        return jsonify({'success': True, 'courses': courses, 'exams': exams})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/upcoming_exams', methods=['POST'])
def upcoming_exams():
    """Return upcoming same-semester exams after the selected exam."""
    data = request.json or {}
    exams = data.get('exams', [])
    exam_id = data.get('exam_id', '')
    upcoming = get_upcoming_exams(exams, exam_id)
    return jsonify({'success': True, 'upcoming_exams': upcoming})


@app.route('/api/analyze', methods=['POST'])
def analyze():
    """Analyze results for a range of PRNs."""
    data = request.json
    project_id = data.get('project_id', '3')
    cs_id = data.get('cs_id', '421')
    exam_id = data.get('exam_id', '')
    prn_start = data.get('prn_start', '')
    prn_end = data.get('prn_end', '')
    delay = float(data.get('delay', 0.5))
    check_upcoming = bool(data.get('check_upcoming', False))
    upcoming_exams_list = data.get('upcoming_exams', [])

    if not exam_id or not prn_start or not prn_end:
        return jsonify({'success': False, 'error': 'Missing required fields'})

    # Validate PRN format (12 digits)
    if not prn_start.isdigit() or not prn_end.isdigit():
        return jsonify({'success': False, 'error': 'PRN must be numeric'})

    if len(prn_start) != 12 or len(prn_end) != 12:
        return jsonify({'success': False, 'error': 'PRN must be exactly 12 digits'})

    start = int(prn_start)
    end = int(prn_end)

    if start > end:
        return jsonify({'success': False, 'error': 'Start PRN must be less than or equal to End PRN'})

    if end - start > 500:
        return jsonify({'success': False, 'error': 'Range too large. Maximum 500 students at a time.'})

    url = f"{BASE_URL}/{project_id}/{cs_id}"
    session = requests.Session()
    # Initialize session with a GET request
    session.get(url, timeout=20, verify=False)

    results = []
    total = end - start + 1

    for i, prn_num in enumerate(range(start, end + 1)):
        prn = str(prn_num).zfill(12)
        result = fetch_result(session, url, cs_id, exam_id, prn)
        results.append(result)

        if delay > 0 and i < total - 1:
            time.sleep(delay)

    analysis = analyze_results(results)
    analysis['upcoming_checked'] = False

    if check_upcoming and analysis['failed_students']:
        if not upcoming_exams_list:
            _, all_exams = get_courses_and_exams(project_id, cs_id)
            upcoming_exams_list = get_upcoming_exams(all_exams, exam_id)

        if upcoming_exams_list:
            tracking = check_upcoming_for_failed_students(
                session, url, cs_id, analysis['failed_students'], upcoming_exams_list, delay
            )
            analysis = apply_upcoming_tracking(analysis, tracking, upcoming_exams_list)

    return jsonify({'success': True, 'analysis': analysis})


@app.route('/api/check_upcoming', methods=['POST'])
def check_upcoming():
    """Check upcoming exams for already-identified failed students."""
    data = request.json or {}
    project_id = data.get('project_id', '3')
    cs_id = data.get('cs_id', '421')
    exam_id = data.get('exam_id', '')
    analysis = data.get('analysis')
    upcoming_exams_list = data.get('upcoming_exams', [])
    delay = float(data.get('delay', 0.5))

    if not analysis or not analysis.get('failed_students'):
        return jsonify({'success': False, 'error': 'No failed students to check'})

    url = f"{BASE_URL}/{project_id}/{cs_id}"
    session = requests.Session()
    session.get(url, timeout=20, verify=False)

    if not upcoming_exams_list:
        _, all_exams = get_courses_and_exams(project_id, cs_id)
        upcoming_exams_list = get_upcoming_exams(all_exams, exam_id)

    if not upcoming_exams_list:
        return jsonify({'success': False, 'error': 'No upcoming examinations found for this semester'})

    tracking = check_upcoming_for_failed_students(
        session, url, cs_id, analysis['failed_students'], upcoming_exams_list, delay
    )
    analysis = apply_upcoming_tracking(analysis, tracking, upcoming_exams_list)
    return jsonify({'success': True, 'analysis': analysis})


def group_exams_by_semester(exams):
    """Group a course's exams by canonical semester number (1..10)."""
    grouped = defaultdict(list)
    for ex in exams:
        sem_num = extract_semester_number(ex.get('text', ''))
        key = sem_num if sem_num is not None else 999
        grouped[key].append(ex)
    return dict(sorted(grouped.items(), key=lambda x: x[0]))


def _scan_prn_all_semesters(url, cs_id, prn, sem_groups, delay=0.1, check_upcoming=True):
    """Fetch all semester results (and upcoming clearances) for a single PRN."""
    session = requests.Session()
    session.get(url, timeout=20, verify=False)

    student_name = ''
    semester_results = {}

    for sem_num, sem_exams in sem_groups.items():
        initial_res = None
        initial_idx = None

        for idx, exam in enumerate(sem_exams):
            res = fetch_result(session, url, cs_id, exam['value'], prn)
            if delay > 0:
                time.sleep(delay)
            if res.get('status') == 'found' and res.get('subjects'):
                initial_res = res
                initial_idx = idx
                if not student_name and res.get('name'):
                    student_name = res['name']
                break

        if not initial_res:
            continue

        failed_subjects = [s.get('name', 'Unknown') for s in initial_res['subjects'] if s.get('failed')]
        sem_entry = {
            'semester': sem_num,
            'exam_id': sem_exams[initial_idx]['value'],
            'exam_name': sem_exams[initial_idx]['text'],
            'subjects': initial_res['subjects'],
            'failed_subjects': failed_subjects,
            'paper_statuses': [],
            'still_failed_subjects': list(failed_subjects),
            'all_cleared': len(failed_subjects) == 0,
        }

        if check_upcoming and failed_subjects and initial_idx + 1 < len(sem_exams):
            remaining_exams = sem_exams[initial_idx + 1:]
            tracking = check_upcoming_for_failed_students(
                session, url, cs_id,
                [{'prn': prn, 'failed_subjects': failed_subjects}],
                remaining_exams, delay
            )
            papers = tracking.get(prn, {})
            paper_statuses = []
            still_failed = []
            for subj_name in failed_subjects:
                info = papers.get(subj_name, {
                    'subject': subj_name, 'cleared': False, 'cleared_in': None,
                    'cleared_exam_id': None, 'new_external': '', 'new_internal': '',
                    'new_grade': '', 'new_total': '', 'attempts': []
                })
                paper_statuses.append(info)
                if not info['cleared']:
                    still_failed.append(subj_name)
            sem_entry['paper_statuses'] = paper_statuses
            sem_entry['still_failed_subjects'] = still_failed
            sem_entry['all_cleared'] = len(still_failed) == 0

        semester_results[str(sem_num)] = sem_entry

    return {
        'prn': prn,
        'name': student_name,
        'semester_results': semester_results,
    }


@app.route('/api/analyze_all_semesters', methods=['POST'])
def analyze_all_semesters():
    """Analyze all semesters in a course for a PRN range."""
    data = request.json or {}
    project_id = data.get('project_id', '3')
    cs_id = data.get('cs_id', '421')
    prn_start = data.get('prn_start', '')
    prn_end = data.get('prn_end', '')
    delay = float(data.get('delay', 0.1))
    check_upcoming = bool(data.get('check_upcoming', True))

    if not prn_start or not prn_end:
        return jsonify({'success': False, 'error': 'Missing PRN range'})
    if not prn_start.isdigit() or not prn_end.isdigit():
        return jsonify({'success': False, 'error': 'PRN must be numeric'})
    if len(prn_start) != 12 or len(prn_end) != 12:
        return jsonify({'success': False, 'error': 'PRN must be exactly 12 digits'})

    start = int(prn_start)
    end = int(prn_end)
    if start > end:
        return jsonify({'success': False, 'error': 'Start PRN must be less than or equal to End PRN'})
    if end - start > 100:
        return jsonify({'success': False, 'error': 'All-Semester scan supports up to 100 PRNs at a time.'})

    url = f"{BASE_URL}/{project_id}/{cs_id}"
    _, all_exams = get_courses_and_exams(project_id, cs_id)
    sem_groups = group_exams_by_semester(all_exams)

    prn_list = [str(n).zfill(12) for n in range(start, end + 1)]
    student_records = []

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {
            executor.submit(_scan_prn_all_semesters, url, cs_id, prn, sem_groups, delay, check_upcoming): prn
            for prn in prn_list
        }
        for fut in as_completed(futures):
            student_records.append(fut.result())

    student_records.sort(key=lambda s: s['prn'])

    # Aggregate semester-wise & overall course statistics
    sem_stats = {}
    students_summary = []
    students_found = 0
    students_all_clear = 0
    students_with_pending = 0
    total_pending_papers = 0

    for stu in student_records:
        sem_res = stu['semester_results']
        if not sem_res:
            continue

        students_found += 1
        total_initial_fail = 0
        total_cleared = 0
        total_still_failed = 0

        for sem_key, sr in sem_res.items():
            if sem_key not in sem_stats:
                sem_num_int = int(sem_key)
                sem_stats[sem_key] = {
                    'semester': sem_num_int,
                    'semester_label': f"Semester {sem_num_int}" if sem_num_int != 999 else 'Other Exams',
                    'results_found': 0,
                    'initial_failures': 0,
                    'cleared_all': 0,
                    'still_failed': 0,
                    'subject_failures': defaultdict(lambda: {'count': 0, 'cleared_later': 0, 'still_failed': 0})
                }
            st = sem_stats[sem_key]
            st['results_found'] += 1

            init_f = len(sr['failed_subjects'])
            still_f = len(sr['still_failed_subjects'])
            cleared_f = init_f - still_f

            total_initial_fail += init_f
            total_cleared += cleared_f
            total_still_failed += still_f

            if init_f > 0:
                st['initial_failures'] += 1
                if sr['all_cleared']:
                    st['cleared_all'] += 1
                else:
                    st['still_failed'] += 1

                cleared_set = {p['subject'] for p in sr.get('paper_statuses', []) if p.get('cleared')}
                for subj_name in sr['failed_subjects']:
                    sf = st['subject_failures'][subj_name]
                    sf['count'] += 1
                    if subj_name in cleared_set:
                        sf['cleared_later'] += 1
                    else:
                        sf['still_failed'] += 1

        if total_still_failed == 0:
            students_all_clear += 1
        else:
            students_with_pending += 1
            total_pending_papers += total_still_failed

        students_summary.append({
            'prn': stu['prn'],
            'name': stu['name'],
            'semesters_appeared': len(sem_res),
            'total_failed_initial': total_initial_fail,
            'total_cleared': total_cleared,
            'total_still_failed': total_still_failed,
            'semester_results': sem_res,
        })

    semesters_list = []
    for sem_key in sorted(sem_stats.keys(), key=lambda k: int(k)):
        st = sem_stats[sem_key]
        st['subject_failures_sorted'] = sorted(
            ((k, dict(v)) for k, v in st['subject_failures'].items()),
            key=lambda x: x[1]['count'],
            reverse=True
        )
        del st['subject_failures']
        semesters_list.append(st)

    return jsonify({
        'success': True,
        'course_analysis': {
            'overall': {
                'total_prns_checked': len(prn_list),
                'students_found': students_found,
                'students_all_clear': students_all_clear,
                'students_with_pending': students_with_pending,
                'total_pending_papers': total_pending_papers,
                'semesters_with_results': len(semesters_list),
            },
            'semesters': semesters_list,
            'students': students_summary,
        }
    })


@app.route('/api/analyze_stream', methods=['POST'])
def analyze_stream():
    """
    Analyze results for a range of PRNs (non-streaming version that returns all at once).
    For the streaming version, we use SSE in a separate endpoint.
    """
    return analyze()


@app.route('/api/single_result', methods=['POST'])
def single_result():
    """Fetch a single result for testing PRN format."""
    data = request.json
    project_id = data.get('project_id', '3')
    cs_id = data.get('cs_id', '421')
    exam_id = data.get('exam_id', '')
    prn = data.get('prn', '')

    url = f"{BASE_URL}/{project_id}/{cs_id}"
    session = requests.Session()
    session.get(url, timeout=20, verify=False)

    # Get raw HTML first
    data_post = {
        'cs_id': cs_id,
        'exam_id': exam_id,
        'prn': prn,
        'btnresult': 'Get Result'
    }
    resp = session.post(url, data=data_post, timeout=20, verify=False)

    # Parse with our parser
    result = fetch_result(session, url, cs_id, exam_id, prn)

    # Also do a raw table extraction for debugging
    soup = BeautifulSoup(resp.text, 'lxml')
    raw_tables = []
    for i, table in enumerate(soup.find_all('table')):
        rows = table.find_all('tr')
        if len(rows) > 1:
            table_data = []
            for row in rows:
                cells = row.find_all(['th', 'td'])
                cell_texts = [c.get_text(strip=True) for c in cells]
                if any(cell_texts):
                    table_data.append(cell_texts)
            if table_data:
                raw_tables.append({'index': i, 'rows': table_data})

    return jsonify({
        'success': True,
        'result': result,
        'raw_tables': raw_tables,
        'raw_html_preview': resp.text if len(resp.text) > 0 else '',
        'page_text': soup.get_text()[:3000]
    })


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    debug = os.environ.get('FLASK_DEBUG', '1') == '1'
    print("=" * 60)
    print("  MGU Pareeksha Result Analyzer")
    print(f"  Open http://localhost:{port} in your browser")
    print("=" * 60)
    app.run(host='0.0.0.0', port=port, debug=debug)
