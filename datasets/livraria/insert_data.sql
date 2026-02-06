-- AUTORES (~10)
INSERT INTO autores (autor_id, nome, pais_origem) VALUES
(1, 'Machado de Assis', 'Brasil'),
(2, 'Clarice Lispector', 'Brasil'),
(3, 'MACHADO de Assis', 'Brasil'), -- variação suja (duplicata leve de nome)
(4, 'Jose Saramago', 'Portugal'), -- duplo espaço + acento faltando
(5, 'José Saramago', 'Portugal'),
(6, 'Isaac Asimov', 'EUA'),
(7, 'George Orwell', 'Reino Unido'),
(8, 'Agatha Christie', 'Reino Unido'),
(9, 'Liu Cixin', 'China'),
(10,'Neil Gaiman', 'Reino Unido')
ON CONFLICT (autor_id) DO NOTHING; -- duplo espaço


-- EDITORAS (~7)
INSERT INTO editoras (editora_id, nome) VALUES
(1, 'Companhia das Letras'),
(2, 'Record'),
(3, 'Rocco'),
(4, 'Alfaguara'),
(5, 'HarperCollins Brasil'),
(6, 'Seguinte'),
(7, 'Editora Técnica')
ON CONFLICT (editora_id) DO NOTHING;


-- GÊNEROS (~12)
INSERT INTO generos (genero_id, nome) VALUES
(1, 'Ficção Científica'),
(2, 'Fantasia'),
(3, 'Romance'),
(4, 'Não Ficção'),
(5, 'Biografia'),
(6, 'História'),
(7, 'Tecnologia'),
(8, 'Educação'),
(9, 'Mistério'),
(10, 'Thriller'),
(11, 'Poesia'),
(12, 'Infantojuvenil')
ON CONFLICT (genero_id) DO NOTHING;


-- LIVROS (~30) (títulos com leves variações de espaços/caixa; alguns ISBN/ano NULL)
INSERT INTO livros (livro_id, titulo, ano_publicacao, isbn, autor_id, editora_id, genero_id, preco) VALUES
(1, 'Dom Casmurro', 1899, '978-85-12345-01-0', 1, 1, 3, 39.90),
(2, 'Memórias Póstumas de Brás Cubas', 1881, '978-85-12345-02-7', 1, 1, 3, 44.90),
(3, 'A Hora da Estrela', 1977, '978-85-12345-03-4', 2, 2, 3, 29.90),
(4, 'Ensaio sobre a Cegueira',1995, '978-85-12345-04-1', 5, 4, 3, 49.90),
(5, 'Quincas Borba', 1891, NULL, 3, 2, 3, 34.50), -- autor_id=3 (nome sujo)
(6, 'Objetos Cortantes', 2006, '978-85-12345-05-8', 8, 2,10, 42.00),
(7, 'Assassinato no Expresso do Oriente', 1934, NULL, 8, 2, 9, 33.00),
(8, '1984', 1949, '978-85-12345-06-5', 7, 5,10, 38.00),
(9, 'Fundação', 1951, '978-85-12345-07-2', 6, 5, 1, 36.00),
(10, 'Eu, Robô', 1950, NULL, 6, 5, 1, 28.00),
(11, 'O Problema dos 3 Corpos', 2006, '978-85-12345-08-9', 9, 5, 1, 55.00),
(12, 'A Revolta de Atlas', 1957, NULL, 7, 5, 4, 52.00),
(13, 'Coraline', 2002, '978-85-12345-09-6',10, 3,12, 27.90),
(14, 'Mitologia Nórdica', 2017, '978-85-12345-10-2',10, 3, 6, 45.00),
(15, 'Poemas Escolhidos', NULL, NULL, 2, 1,11, 19.90),
(16, 'Ficção Científica Para Iniciantes', 2020, NULL, 6, 7, 1, 31.00),
(17, 'História do Brasil', 2010, '978-85-12345-11-9', 1, 1, 6, 60.00),
(18, 'Didática da Educação', 2015, NULL, 2, 6, 8, 50.00),
(19, 'Algoritmos e Estruturas de Dados', 2012, '978-85-12345-12-6', 6, 7, 7, 70.00),
(20, 'Romance sem Título', NULL, NULL, 4, 4, 3, 21.00),
(21, 'Mistério na Biblioteca', 2021, NULL, 8, 2, 9, 37.00),
(22, 'Trilogia da Fundação', 1966, NULL, 6, 5, 1, 59.00),
(23, 'Contos de Fantasia', 2009, '978-85-12345-13-3',10, 3, 2, 32.00),
(24, 'Biografia de um Autor', 2001, NULL, 5, 4, 5, 26.00),
(25, 'Manual de Tecnologia', 2018, '978-85-12345-14-0', 6, 7, 7, 64.00),
(26, 'Educação Moderna', 2022, NULL, 2, 6, 8, 48.00),
(27, 'Poesia Reunida', 1999, NULL, 2, 2,11, 22.00),
(28, 'Fantasia & Magia', 2005, '978-85-12345-15-7',10, 3, 2, 35.00),
(29, 'O Enigma do Tempo', 2013, NULL, 7, 5,10, 41.00),
(30, 'Clássicos Infantojuvenis', 1995, '978-85-12345-16-4',10, 3,12, 29.00)
ON CONFLICT (livro_id) DO NOTHING;


-- MEMBROS (~20) (alguns e-mails problemáticos/maiúsculos)
INSERT INTO membros (membro_id, nome, email, data_cadastro) VALUES
(1, 'Ana Souza', 'ana@example.com', '2023-01-10'),
(2, 'João Silva', 'joao.silva@example.com', '2023-02-05'),
(3, 'MARIA de Souza', 'maria@@mail.com', '2023-03-15'),
(4, 'carlos almeida', 'CARLOS@EXEMPLO.COM', '2023-04-20'),
(5, 'Beatriz Lima', NULL, '2023-05-12'),
(6, 'Diego Santos', 'diego.santos@example.com', '2023-06-01'),
(7, 'Eduarda Martins', 'eduarda.martins@example.com', '2023-07-22'),
(8, 'felipe carvalho', 'felipe@example.com', '2023-08-03'),
(9, 'Gustavo Ribeiro', NULL, '2023-09-09'),
(10, 'Helena Rocha', 'helena.rocha@example.com', '2023-10-10'),
(11, 'Igor Monteiro', 'igormonteiro', '2023-11-11'),
(12, 'Julia Fernandes', 'julia.fernandes@example.com', '2024-01-05'),
(13, 'Karen Araujo', 'karen@example.com', '2024-02-14')
ON CONFLICT (membro_id) DO NOTHING;


-- EMPRÉSTIMOS
INSERT INTO emprestimos (emprestimo_id, livro_id, membro_id, data_emprestimo, data_devolucao) VALUES
(1, 1, 1, '2024-01-15', '2024-02-01'),
(2, 3, 2, '2024-01-20', NULL),
(3, 8, 4, '2024-02-10', '2024-02-25'),
(4, 13, 5, '2024-03-05', NULL),
(5, 9, 7, '2024-03-12', '2024-03-28')
ON CONFLICT (emprestimo_id) DO NOTHING;
