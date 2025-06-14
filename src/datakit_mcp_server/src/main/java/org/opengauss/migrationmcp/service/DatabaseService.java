package org.opengauss.migrationmcp.service;

import io.restassured.RestAssured;
import io.restassured.http.Header;
import io.restassured.response.Response;
import org.hamcrest.Matchers;
import org.opengauss.migrationmcp.dto.TargetDataBaseDto;
import org.opengauss.migrationmcp.entity.SourceDatabaseVo;
import org.opengauss.migrationmcp.entity.SourceDatabaseClusterVo;
import org.opengauss.migrationmcp.entity.TargetDatabaseVo;
import org.opengauss.migrationmcp.entity.TargetDatabaseClusterVo;
import org.opengauss.migrationmcp.utils.RestAssuredUtils;
import org.springframework.ai.tool.annotation.Tool;
import org.springframework.ai.tool.annotation.ToolParam;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Service;

import java.net.ConnectException;
import java.util.ArrayList;
import java.util.List;

@Service
public class DatabaseService {
    @Autowired
    private Header tokenHeader;

    private String basePath = "/plugins/data-migration/resource";

    @Tool(name = "源端数据库列表", description = "用于获取源端数据库信息列表，即获取MySQL数据库信息列表")
    public List<SourceDatabaseVo> getSourceDatabaseList() {
        List<SourceDatabaseVo> sourceDatabaseVoList = doGetSourceDatabaseList();
        sourceDatabaseVoList.forEach(sourceDatabaseVo -> sourceDatabaseVo.setPassword("******"));

        return sourceDatabaseVoList;
    }

    @Tool(name = "目标端数据库列表", description = "用于获取目标端数据库信息列表，即获取openGauss数据库信息列表")
    public List<TargetDatabaseVo> getTargetDatabaseList() {
        List<TargetDatabaseVo> targetDatabaseVoList = doGetTargetDatabaseList();
        targetDatabaseVoList.forEach(targetDatabaseVo -> targetDatabaseVo.setPassword("******"));

        return targetDatabaseVoList;
    }

    @Tool(name = "源端数据库包含的database列表", description = "用于获取源端数据库下的database列表")
    public List<String> getSourceDatabaseNameList(
            @ToolParam(required = true, description = "数据库所在主机ip") String ip,
            @ToolParam(required = true, description = "数据库端口") String port
    ) {
        SourceDatabaseVo sourceDatabaseVo = null;

        List<SourceDatabaseVo> sourceDatabaseList = doGetSourceDatabaseList();
        for (SourceDatabaseVo databaseVo : sourceDatabaseList) {
            if (databaseVo.getIp().equals(ip) && databaseVo.getPort().equals(port)) {
                sourceDatabaseVo = databaseVo;
                break;
            }
        }

        if (sourceDatabaseVo == null) {
            throw new RuntimeException("无对应的源端数据库信息，请确保数据库已存在");
        }

        RestAssuredUtils.setBasePath(basePath);

        Response response;
        try {
            response = RestAssured.given()
                    .header(tokenHeader)
                    .contentType("application/x-www-form-urlencoded; charset=UTF-8")
                    .formParam("url", sourceDatabaseVo.getUrl())
                    .formParam("username", sourceDatabaseVo.getUsername())
                    .formParam("password", sourceDatabaseVo.getPassword())
                    .when()
                    .post("/getSourceClusterDbs");
        } catch (Exception e) {
            if (e instanceof ConnectException) {
                throw new RuntimeException("无法连接到DataKit服务，请检查服务是否正在运行");
            } else {
                throw new RuntimeException(e);
            }
        }

        response.then().body("code", Matchers.equalTo(200));

        return response.jsonPath().getList("data", String.class);
    }

    @Tool(name = "目标端数据库包含的database列表", description = "用于获取目标端数据库下的database列表")
    public List<String> getTargetDatabaseNameList(
            @ToolParam(required = true, description = "数据库所在主机ip") String ip,
            @ToolParam(required = true, description = "数据库端口") String port
    ) {
        TargetDatabaseVo targetDatabaseVo = null;

        List<TargetDatabaseVo> targetDatabaseList = doGetTargetDatabaseList();
        for (TargetDatabaseVo databaseVo : targetDatabaseList) {
            if (databaseVo.getIp().equals(ip) && databaseVo.getPort().equals(port)) {
                targetDatabaseVo = databaseVo;
                break;
            }
        }

        if (targetDatabaseVo == null) {
            throw new RuntimeException("无对应的目标端数据库信息，请确保数据库已存在");
        }

        RestAssuredUtils.setBasePath(basePath);

        Response response;
        try {
            response = RestAssured.given()
                    .header(tokenHeader)
                    .header("Content-Type", "application/json")
                    .body(targetDatabaseVo)
                    .when()
                    .post("/getTargetClusterDbs");
        } catch (Exception e) {
            if (e instanceof ConnectException) {
                throw new RuntimeException("无法连接到DataKit服务，请检查服务是否正在运行");
            } else {
                throw new RuntimeException(e);
            }
        }

        response.then().body("code", Matchers.equalTo(200));

        List<TargetDataBaseDto> targetDataBaseDtos = response.jsonPath().getList("data", TargetDataBaseDto.class);
        return targetDataBaseDtos.stream().map(TargetDataBaseDto::getDbName).toList();
    }

    public List<SourceDatabaseVo> doGetSourceDatabaseList() {
        List<SourceDatabaseVo> sourceDatabaseVoList = new ArrayList<>();

        List<SourceDatabaseClusterVo> sourceDatabaseClusterVos = getSourceDatabaseClusters();
        sourceDatabaseClusterVos.forEach(clusterVo -> {
            sourceDatabaseVoList.addAll(clusterVo.getNodes());
        });

        return sourceDatabaseVoList;
    }

    public List<TargetDatabaseVo> doGetTargetDatabaseList() {
        List<TargetDatabaseVo> targetDatabaseVoList = new ArrayList<>();

        List<TargetDatabaseClusterVo> targetDatabaseClusterVos = getTargetDatabaseClusters();
        targetDatabaseClusterVos.forEach(clusterVo -> {
            targetDatabaseVoList.addAll(clusterVo.getClusterNodes());
        });

        return targetDatabaseVoList;
    }

    private List<SourceDatabaseClusterVo> getSourceDatabaseClusters() {
        RestAssuredUtils.setBasePath(basePath);

        Response response;
        try {
            response = RestAssured.given()
                    .header(tokenHeader)
                    .when()
                    .get("/sourceClusters");
        } catch (Exception e) {
            if (e instanceof ConnectException) {
                throw new RuntimeException("无法连接到DataKit服务，请检查服务是否正在运行");
            } else {
                throw new RuntimeException(e);
            }
        }

        response.then().body("code", Matchers.equalTo(200));

        return response.jsonPath().getList("data.sourceClusters", SourceDatabaseClusterVo.class);
    }

    private List<TargetDatabaseClusterVo> getTargetDatabaseClusters() {
        RestAssuredUtils.setBasePath(basePath);

        Response response;
        try {
            response = RestAssured.given()
                    .header(tokenHeader)
                    .when()
                    .get("/targetClusters");
        } catch (Exception e) {
            if (e instanceof ConnectException) {
                throw new RuntimeException("无法连接到DataKit服务，请检查服务是否正在运行");
            } else {
                throw new RuntimeException(e);
            }
        }

        response.then().body("code", Matchers.equalTo(200));

        return response.jsonPath().getList("data.targetClusters", TargetDatabaseClusterVo.class);
    }
}
