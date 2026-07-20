const CubejsServer = require("@cubejs-backend/server");

const server = new CubejsServer();

server.listen().then(({ version, port }) => {
  console.log(`Cube ${version} is listening on ${port}`);
});
