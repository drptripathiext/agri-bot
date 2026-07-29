"""ARS / ASRB / SMS / STO interview coaching mode."""
import os, re

_HERE = os.path.dirname(os.path.abspath(__file__))

BLUEPRINT = ""
for p in (os.path.join(_HERE, "interview.txt"),
          os.path.join(os.getcwd(), "interview.txt")):
    if os.path.exists(p):
        BLUEPRINT = open(p, encoding="utf-8").read().strip()
        break

# Sawaal interview ke baare me hai ya nahi
IS_INTERVIEW = re.compile(r"""(?ix)
    \b(
        interview | interviews | viva | \s*voce
      | (?:selection|interview)\s*board
      | personality\s*test
      | panel\s*(?:question|round|member)
      | mock\s*(?:interview|test\s*interview)
      | intrview | interveiw | intervew
    )\b
  | \b(?:ars|asrb|sms|sto|icar|net)\b[^?.\n]{0,25}\b(?:interview|viva|panel|board)\b
  | \b(?:interview|viva)\b[^?.\n]{0,25}\b(?:ars|asrb|sms|sto|icar|prepare|tips|crack)\b
  | tell\s+(?:us|me)\s+about\s+yourself
  | why\s+(?:should\s+we\s+select\s+you|icar|ars)
  | \b(?:my|your|future)\s+research\s+vision\b
""")

# Ye sawaal interview-prep ke nahi, exam-admin ke hain — inhe normal treat karo
NOT_INTERVIEW = re.compile(r"(?i)\b(schedule|date|dates|admit\s*card|result|results|"
                           r"notification|vacancy|vacancies|apply|application|"
                           r"last\s*date|cutoff|cut\s*off)\b")


def is_interview(q):
    return bool(IS_INTERVIEW.search(q)) and not NOT_INTERVIEW.search(q)

SYSTEM = """You are {name}, acting as a senior ICAR Agricultural Scientist and interview
mentor. The student is preparing for the ASRB / ARS / SMS / STO interview (personality
test) and has come to you for guidance.

{lang_rule}

You have a proven interview blueprint below. Use it as your framework, and add your own
expertise as a scientist who has sat on both sides of the table.

=== INTERVIEW BLUEPRINT ===
{blueprint}
=== END BLUEPRINT ===

HOW TO COACH:
1. Be warm, encouraging and practical — like a senior guiding a junior, not a textbook.
2. Give SPECIFIC, actionable advice. Model answers, exact phrasing, what the panel is
   really testing behind each question, and the common mistakes candidates make.
3. When the student asks about a particular question ("Tell us about yourself",
   "Why ICAR?"), give: what the panel wants → a structure to follow → a short sample
   answer → the trap to avoid.
4. Ground everything in Indian agriculture: ICAR's mandate and vision, NARES, KVKs,
   national priorities (doubling farmer income, climate-resilient agriculture, natural
   farming, millets, digital agriculture, nutrition security), and current schemes.
5. If the student mentions their own discipline, thesis or research, tailor the advice
   to it and predict the actual questions the panel will ask them.
6. Keep it tight — bullets or short blocks, bold the key terms, under 250 words unless
   the student asks for a detailed answer or a mock session.
7. Never name your sources, never mention notes, documents, AI, Google or Gemini.
8. End with one concrete next step the student can do today.
9. No preamble like "Sure" or "Great question". Start with the substance."""


def blueprint_summary():
    return (
        "🎯 <b>Mastering the ARS / ASRB Interview</b>\n\n"
        "It is not about knowing every answer. It is about showing <b>scientific "
        "thinking, confidence, communication and suitability</b> as a scientist.\n\n"
        "<b>1. Your CV is your first question paper</b>\n"
        "Every line — Ph.D., M.Sc. thesis, publications, projects, awards, trainings — "
        "can become a question. Never write what you cannot explain.\n\n"
        "<b>2. Be the expert of your own research</b>\n"
        "Why this topic? What was the research gap? Why this methodology? Why these "
        "statistics? How will farmers benefit? They are testing <i>how you think</i>.\n\n"
        "<b>3. Revise the fundamentals</b>\n"
        "Core concepts · research methodology · statistics · recent advances · "
        "national agricultural priorities.\n\n"
        "<b>4. Connect science with society</b>\n"
        "How your work solves field problems, benefits farmers, supports sustainable "
        "agriculture, and aligns with ICAR's vision.\n\n"
        "<b>5. Stay updated</b>\n"
        "Agriculture · livestock · climate change · AI &amp; digital agriculture · "
        "drones · ICT in extension · government schemes.\n\n"
        "<b>6. Communication is your hidden score</b>\n"
        "Listen · pause · speak clearly · eye contact · be concise. A confident short "
        "answer beats a hesitant long one.\n\n"
        "<b>7. If you don't know, say so</b>\n"
        "Admit it politely. Honesty earns more respect than wild guessing.\n\n"
        "<b>8. They want a scientist, not an encyclopedia</b>\n"
        "Sound · curious · analytical · practical · honest · willing to learn.\n\n"
        "<b>9. The one tip that changes everything</b>\n"
        "Record yourself answering: <i>Tell us about yourself · Explain your Ph.D. in "
        "2 minutes · Why ICAR? · Why should we select you? · Your research vision.</i>\n\n"
        "———\n"
        "Ask me anything about the interview — for example:\n"
        "• <code>How do I answer \"Tell us about yourself\"?</code>\n"
        "• <code>What will the panel ask about my Extension thesis?</code>\n"
        "• <code>Why ICAR — give me a strong answer</code>\n"
        "• <code>/mock</code> — a full mock interview round (Agricultural Extension)\n"
        "• <code>/mock ICT in extension</code> — mock round on a chosen topic"
    )


MOCK_SYSTEM = """You are a senior ICAR ASRB interview panel member conducting a mock
interview for an Agricultural Extension candidate.

Ask exactly 6 questions, numbered, in this order:
1. An opening/personal question (tell us about yourself / why ARS)
2. A question on their core discipline fundamentals
3. A research-methodology or statistics question
4. A question linking research to farmers and field impact
5. A current-affairs / national-priority question in Indian agriculture
6. A situational or judgement question a scientist would face

After the questions, add a short section "WHAT THE PANEL IS LOOKING FOR" with one
crisp line per question. Use English. Be realistic and exam-standard.
{topic_line}"""
