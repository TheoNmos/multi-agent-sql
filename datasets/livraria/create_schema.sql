CREATE TABLE if not exists autores (
autor_id SERIAL PRIMARY KEY,
nome TEXT NOT NULL,
pais_origem TEXT
);


CREATE TABLE if not exists editoras (
editora_id SERIAL PRIMARY KEY,
nome TEXT NOT NULL
);


CREATE TABLE if not exists generos (
genero_id SERIAL PRIMARY KEY,
nome TEXT NOT NULL UNIQUE
);


CREATE TABLE if not exists livros (
livro_id SERIAL PRIMARY KEY,
titulo TEXT NOT NULL,
ano_publicacao INT,
isbn TEXT,
autor_id INT NOT NULL REFERENCES autores(autor_id),
editora_id INT REFERENCES editoras(editora_id),
genero_id INT REFERENCES generos(genero_id),
preco NUMERIC(10,2)
);


-- ISBN único quando presente
CREATE UNIQUE INDEX IF NOT EXISTS livros_isbn_unq ON livros(isbn) WHERE isbn IS NOT NULL;


CREATE TABLE if not exists membros (
membro_id SERIAL PRIMARY KEY,
nome TEXT NOT NULL,
email TEXT,
data_cadastro DATE NOT NULL
);


CREATE TABLE if not exists emprestimos (
emprestimo_id SERIAL PRIMARY KEY,
livro_id INT NOT NULL REFERENCES livros(livro_id),
membro_id INT NOT NULL REFERENCES membros(membro_id),
data_emprestimo DATE NOT NULL,
data_devolucao DATE
);


-- Índices leves
CREATE INDEX IF NOT EXISTS idx_livros_titulo ON livros (titulo);
CREATE INDEX IF NOT EXISTS idx_livros_autor ON livros (autor_id);
CREATE INDEX IF NOT EXISTS idx_emp_membro ON emprestimos (membro_id);
CREATE INDEX IF NOT EXISTS idx_emp_livro ON emprestimos (livro_id);
