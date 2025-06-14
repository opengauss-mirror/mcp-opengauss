package org.opengauss.migrationmcp.service;

import io.restassured.RestAssured;
import io.restassured.http.Header;
import io.restassured.response.Response;
import org.hamcrest.Matchers;
import org.opengauss.migrationmcp.utils.RestAssuredUtils;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Service;

import java.net.ConnectException;
import java.util.List;

@Service
public class PortalHostService {
    @Autowired
    private Header tokenHeader;

    private final String basePath = "/plugins/data-migration/resource";

    public List<String> getPortalHostIds() {
        RestAssuredUtils.setBasePath(basePath);

        Response response;
        try {
            response = RestAssured.given()
                    .header(tokenHeader)
                    .when()
                    .get("/getHosts");
        } catch (Exception e) {
            if (e instanceof ConnectException) {
                throw new RuntimeException("无法连接到DataKit服务，请检查服务是否正在运行");
            } else {
                throw new RuntimeException(e);
            }
        }

        response.then().body("code", Matchers.equalTo(200));
        List<String> portalHostIdList = response.jsonPath().getList(
                "data.findAll { it.installInfo.installStatus == 2 }.installInfo.runHostId", String.class);
        if (portalHostIdList == null || portalHostIdList.isEmpty()) {
            throw new RuntimeException("无法获取迁移执行机，请确保DataKit中已存在可用的迁移执行机");
        }
        return portalHostIdList;
    }
}
