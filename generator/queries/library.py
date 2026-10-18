SCHEMA_ID = "library"

# Identifiers are unquoted camelCase; PostgreSQL folds them to lowercase.
QUERIES = [
    ("List each book title with the full name of each of its authors.",
     """SELECT b.title, a.fullName
FROM books b
JOIN bookAuthors ba ON ba.bookId = b.id
JOIN authors a ON a.id = ba.authorId""",
     ["m2m"]),

    ("Which books have more than one author? Give the titles.",
     """SELECT b.title
FROM books b
JOIN bookAuthors ba ON ba.bookId = b.id
GROUP BY b.id, b.title
HAVING COUNT(*) > 1""",
     ["m2m", "having"]),

    ("Which authors have written at least one book in the 'Science Fiction' genre? Return distinct author names.",
     """SELECT DISTINCT a.fullName
FROM authors a
JOIN bookAuthors ba ON ba.authorId = a.id
JOIN bookGenres bg ON bg.bookId = ba.bookId
JOIN genres g ON g.id = bg.genreId
WHERE g.name = 'Science Fiction'""",
     ["m2m"]),

    ("Which books are currently checked out, and by which member? Show book title and member name.",
     """SELECT b.title, m.name
FROM loans l
JOIN copies c ON c.id = l.copyId
JOIN books b ON b.id = c.bookId
JOIN members m ON m.id = l.memberId
WHERE l.returnedOn IS NULL""",
     ["nullable", "generic_names"]),

    ("Which loans were overdue as of June 1, 2024 (due before that date and not yet returned)? Return loan ids.",
     """SELECT id FROM loans
WHERE dueOn < DATE '2024-06-01' AND returnedOn IS NULL""",
     ["nullable", "dates"]),

    ("List the names and emails of members who are subscribed to the newsletter.",
     "SELECT name, email FROM members WHERE newsletter = 'on'",
     ["flag_onoff"]),

    ("For each sequel, show its title and the title of the book it follows.",
     """SELECT s.title AS sequel, p.title AS predecessor
FROM books s
JOIN books p ON p.id = s.sequelOfId""",
     ["self_join"]),

    ("Which books start a series: they have a sequel but are not themselves a sequel?",
     """SELECT b.title FROM books b
WHERE b.sequelOfId IS NULL
  AND EXISTS (SELECT 1 FROM books s WHERE s.sequelOfId = b.id)""",
     ["self_join", "nullable"]),

    ("How many loans has each genre had? Show the genre name and loan count.",
     """SELECT g.name, COUNT(*) AS loans
FROM loans l
JOIN copies c ON c.id = l.copyId
JOIN bookGenres bg ON bg.bookId = c.bookId
JOIN genres g ON g.id = bg.genreId
GROUP BY g.id, g.name""",
     ["m2m", "generic_names"]),

    ("List the names of all genres nested anywhere under 'Fiction' (not including Fiction itself).",
     """WITH RECURSIVE sub AS (
    SELECT g.id, g.name FROM genres g
    JOIN genres parent ON parent.id = g.parentGenreId
    WHERE parent.name = 'Fiction'
    UNION ALL
    SELECT g.id, g.name FROM genres g JOIN sub ON g.parentGenreId = sub.id
)
SELECT name FROM sub""",
     ["self_join"]),

    ("Which living authors (no death year recorded) have written more than 3 books?",
     """SELECT a.fullName
FROM authors a
JOIN bookAuthors ba ON ba.authorId = a.id
WHERE a.deathYear IS NULL
GROUP BY a.id, a.fullName
HAVING COUNT(*) > 3""",
     ["m2m", "nullable", "having"]),

    ("What is the average page count of books from each publisher? Show publisher name.",
     """SELECT p.name, AVG(b.pageCount) AS avgPages
FROM publishers p
JOIN books b ON b.publisherId = p.id
GROUP BY p.id, p.name""",
     ["nullable"]),

    ("What are the 5 most borrowed book titles?",
     """SELECT b.title, COUNT(*) AS timesBorrowed
FROM loans l
JOIN copies c ON c.id = l.copyId
JOIN books b ON b.id = c.bookId
GROUP BY b.id, b.title
ORDER BY timesBorrowed DESC
LIMIT 5""",
     ["top_k"]),

    ("Which members have never borrowed anything?",
     """SELECT m.name FROM members m
WHERE NOT EXISTS (SELECT 1 FROM loans l WHERE l.memberId = m.id)""",
     ["anti_join"]),

    ("How many books have no ISBN?",
     "SELECT COUNT(*) FROM books WHERE isbn IS NULL",
     ["nullable"]),

    ("What is the average loan length in days for loans that have been returned?",
     """SELECT AVG(returnedOn - loanedOn) AS avgDays
FROM loans WHERE returnedOn IS NOT NULL""",
     ["dates", "nullable"]),

    ("How many circulating copies does each branch hold?",
     """SELECT branch, COUNT(*) FROM copies WHERE circulating = 'on' GROUP BY branch""",
     ["flag_onoff"]),

    ("List members with auto-renew turned off who currently have at least one book checked out.",
     """SELECT DISTINCT m.name
FROM members m
JOIN loans l ON l.memberId = m.id
WHERE m.autoRenew = 'off' AND l.returnedOn IS NULL""",
     ["flag_onoff", "nullable"]),

    ("Show each author's name and the title of their first listed book where they are the lead author (authorOrder 1), for books published after 2000.",
     """SELECT a.fullName, b.title
FROM authors a
JOIN bookAuthors ba ON ba.authorId = a.id
JOIN books b ON b.id = ba.bookId
WHERE ba.authorOrder = 1 AND b.publishedYear > 2000""",
     ["m2m"]),

    ("How many books exist in each language, most common first?",
     """SELECT language, COUNT(*) AS n FROM books GROUP BY language ORDER BY n DESC""",
     []),

    ("Which publishers have not published any book in the catalog?",
     """SELECT p.name
FROM publishers p
WHERE NOT EXISTS (SELECT 1 FROM books b WHERE b.publisherId = p.id)""",
     ["anti_join", "nullable"]),

    ("List the ids of books that belong to a series, either because they are a sequel or because another book is a sequel of them.",
     """SELECT id FROM books WHERE sequelOfId IS NOT NULL
UNION
SELECT sequelOfId FROM books WHERE sequelOfId IS NOT NULL""",
     ["self_join", "nullable"]),

    ("Give one combined list of every member's name and every author's full name, keeping duplicates.",
     """SELECT name FROM members
UNION ALL
SELECT fullName FROM authors""",
     ["generic_names"]),
]
