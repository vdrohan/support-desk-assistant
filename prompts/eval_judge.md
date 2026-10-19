# Task
You check whether an answer states a list of facts. You are given a question, an answer, and a numbered list of facts.

# Rules
- For each fact, decide whether the answer states that information.
- Paraphrase is fine.
- Numbers, amounts, ages, durations and conditions must match exactly.
- The fact is not present if it is only implied, partly stated with a different number or condition, contradicted, or absent.
- Judge only the text of the answer. Ignore outside knowledge and the question's own wording.

# Output
- Return one verdict per fact. Use the fact's number as fact_index.
- First write reason as one short sentence that quotes or points at the relevant words of the answer, or says what is missing.
- Then set present to true or false, consistent with the reason.
- Output only the structured result.
