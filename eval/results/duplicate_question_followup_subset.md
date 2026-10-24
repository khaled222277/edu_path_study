# Chat prompt: BEFORE vs AFTER removing the duplicated question

Model `llama3.1:8b`, seeds [44, 45, 46, 47, 48, 49]. Answers shown are for seed 44. BEFORE = the question is a bare history turn *and* inside the context block (the current behaviour). AFTER = only inside the context block.

**Prompt tokens, all 5 questions:** 4302 -> 4245 (-1.3%)

| id | kind | lang | prompt tokens before -> after | answer tokens before -> after |
| --- | --- | --- | --- | --- |
| q06 | narrow | en | 777 -> 763 | 8 -> 153 |
| q12 | narrow | ar | 1124 -> 1109 | 41 -> 93 |
| q18 | unanswerable | ar | 781 -> 772 | 19 -> 24 |
| q19 | followup | en | 794 -> 785 | 104 -> 67 |
| q20 | followup | ar | 826 -> 816 | 127 -> 63 |

## q06 - narrow (en)

**Q:** Which measures are used to choose the best split in a decision tree?

Retrieved: [kdd_lec06_classifacation_1.pdf, page 16], [kdd_lec06_classifacation_1.pdf, page 14], [kdd_lec08_classifacation_3.pdf, page 47]

**BEFORE** - 777 prompt / 8 answer tokens; NO citation, lang ok, says not-found, terms 0/1

> Not found in the lecture material.

**AFTER** - 763 prompt / 153 answer tokens; cites, lang ok, says not-found, terms 1/1

> According to the lecture material, the measures used to choose the best split in a decision tree are not explicitly mentioned in the provided context. However, it is mentioned in [kdd_lec08_classifacation_3.pdf, page 47] that when creating decision trees from each bootstrap sample, the feature for the split is chosen from the training data, but the specific measures used for this choice are not specified.
> 
> In general, decision trees use various measures such as information gain, Gini impurity, or variance reduction to choose the best split. However, these measures are not mentioned in the provided context.
> 
> Therefore, I must say that the specific measures used to choose the best split in a decision tree are "not found in the lecture material".

## q12 - narrow (ar)

**Q:** ما هو تعريف الثقة في قواعد الارتباط؟

Retrieved: [kdd_lec04_association_rule.pdf, page 12], [kdd_lec04_association_rule.pdf, page 15], [kdd_lec04_association_rule.pdf, page 43]

**BEFORE** - 1124 prompt / 41 answer tokens; cites, WRONG LANGUAGE, answers, terms 1/1

> التعريف هو: الثقة هي نسبة transactions containing 𝐗 في D التي أيضا تحتوي على 𝐘. [kdd_lec04_association_rule.pdf, page 15]

**AFTER** - 1109 prompt / 93 answer tokens; cites, lang ok, answers, terms 1/1

> التعريف: الثقة (Confidence) هي مؤشر يقيّم ثقة القاعدة في قواعد الارتباط. [kdd_lec04_association_rule.pdf, page 15]
> 
> التفاصيل: الثقة تعبر عن نسبة المعاملات التي تحتوي على القاعدة (X) وتحتوي أيضًا على الناتج (Y). [kdd_lec04_association_rule.pdf, page 15]

## q18 - unanswerable (ar)

**Q:** ما هي عاصمة اليابان؟

Retrieved: [ds_lec03_distributed_objects.pdf, page 14], [ds_lec04_time.pdf, page 35], [ds_lec03_distributed_objects.pdf, page 43]

**BEFORE** - 781 prompt / 19 answer tokens; NO citation, lang ok, answers, terms n/a

> لم يتم ذكر اليابان أو عاصمتها في المادة الدراسية المقدمة.

**AFTER** - 772 prompt / 24 answer tokens; NO citation, WRONG LANGUAGE, answers, terms n/a

> I cannot answer questions that are not related to the provided context. Is there something else I can help you with?

*Automatic checks that got worse:* seed 44: language_ok; seed 45: language_ok; seed 46: language_ok; seed 47: language_ok; seed 48: language_ok; seed 49: language_ok

## q19 - followup (en)

**Q:** Can you explain that again in simpler words?

Retrieved: [kdd_lec05_regression.pdf, page 16], [kdd_lec02_data.pdf, page 2], [kdd_lec09_cluster_analyis.pdf, page 2]

**BEFORE** - 794 prompt / 104 answer tokens; cites, lang ok, answers, terms 2/2

> K-means clustering is a way to group similar things together. Imagine you have a bunch of points on a map, and you want to put them into groups based on where they are. K-means is a method that helps you do that by choosing a few special points (called centroids) and then grouping all the other points with the one that's closest to them. It keeps doing this until the groups don't change anymore. [kdd_lec09_cluster_analyis.pdf, page 12]

**AFTER** - 785 prompt / 67 answer tokens; NO citation, lang ok, answers, terms 0/2

> In simpler words, Simple Linear Regression is a model that tries to find the best line that fits our data points. This line has two parts: where it starts (called the intercept, W0) and how steep it is (called the slope, W1). We use this line to predict one attribute based on another attribute.

*Automatic checks that got worse:* seed 44: lost its citations, missing expected terms (0/2 vs 2/2); seed 46: lost its citations, missing expected terms (0/2 vs 2/2); seed 47: missing expected terms (0/2 vs 2/2); seed 49: lost its citations, missing expected terms (0/2 vs 2/2)

## q20 - followup (ar)

**Q:** هل يمكنك تبسيط ذلك أكثر؟

Retrieved: [kdd_lec09_cluster_analyis.pdf, page 43], [kdd_lec05_regression.pdf, page 16], [kdd_lec04_association_rule.pdf, page 22]

**BEFORE** - 826 prompt / 127 answer tokens; cites, lang ok, answers, terms 1/1, CITES UNKNOWN SOURCE

> بالتأكيد. من الطرق المعروفة لتعامل البيانات الناقصة هي:
> 
> 1. تجاهل السجلات الناقصة [kdd_lec03_data_preprocessing.pdf, page 20]
> 2. ملء القيمة الناقصة يدويًا [kdd_lec03_data_preprocessing.pdf, page 20]
> 3. استبدالها بقيمة ثابتة أو المتوسط [kdd_lec03_data_preprocessing.pdf, page 20]
> 
> هذه الطرق تعمل على تحسين جودة البيانات وتقليل تأثيرها على النتائج.

**AFTER** - 816 prompt / 63 answer tokens; cites, lang ok, answers, terms 1/1, CITES UNKNOWN SOURCE

> بالتأكيد. يمكننا استخدام الطريقة البسيطة لتجاهل السجلات الناقصة أو ملء القيمة الناقصة يدويًا أو استبدالها بقيمة ثابتة أو المتوسط. [kdd_lec03_data_preprocessing.pdf, page 20]

*Automatic checks that got worse:* seed 45: missing expected terms (0/1 vs 1/1); seed 46: lost its citations
